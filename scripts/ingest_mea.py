"""Batch-ingest .mea files under a folder into the DB. Version 1.1.

Follows DESIGN.md §5 (pipeline steps), §10 (never touch human fields),
§14 (per-file try/except: batch never aborts), §19 (fail-loud parse
errors; warnings for weak-but-parseable data).

Usage:
    python scripts/ingest_mea.py "mea data" \\
        --user shengic --password sirirat --database gc-ims_database
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pymysql
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).parent))
from mea_parser import (  # noqa: E402
    MeaParseError, split_mea, find_rip, promote, sample_type_from_name,
    parse_program, program_hash, parse_telemetry,
)
from mea_preview import max_pool, make_axes, pack_npz  # noqa: E402

POOL_RT = 12
POOL_DT = 6
PIPELINE_VER = 1
# heatmap_png / thumb_png are populated by scripts/render_previews.py in a
# separate pass — the §5 step-8 backfill pattern. Ingest leaves them NULL.


def _upsert_instrument(cur, p):
    cur.execute("SELECT instrument_id FROM instrument WHERE machine_serial=%s",
                (p["machine_serial"],))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("""
        INSERT INTO instrument
            (machine_type, machine_serial, machine_name, adio_serial,
             firmware_version, firmware_date, drift_tube_um, drift_voltage_v, sensor_data)
        VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s)
    """, (p["machine_type"], p["machine_serial"], p["machine_name"], p["adio_serial"],
          p["firmware_version"], p["firmware_date"],
          p["drift_tube_um"], p["drift_voltage_v"], p["sensor_data"]))
    return cur.lastrowid


def _upsert_method(cur, p, warnings):
    prog_name, ok = parse_program(p["program_raw"])
    if not ok:
        warnings.append("program pattern unmatched, stored as '(unparsed)'")
    ph = program_hash(p["program_raw"], p["gc_column"], p["drift_gas"])
    cur.execute("SELECT method_id FROM gc_method WHERE program_hash=%s", (ph,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("""
        INSERT INTO gc_method
            (program_name, program_raw, gc_column, drift_gas,
             flow_ims_setpoint_ml_min, flow_gc_setpoint_ml_min,
             temp_setpoints_c, program_hash)
        VALUES (%s,%s,%s,%s, %s,%s, %s,%s)
    """, (prog_name, p["program_raw"], p["gc_column"], p["drift_gas"],
          p["flow_ims_setpoint_ml_min"], p["flow_gc_setpoint_ml_min"],
          json.dumps(p["temp_setpoints_c"]) if p["temp_setpoints_c"] else None,
          ph))
    return cur.lastrowid


def _upsert_batch_and_link(cur, folder_name: str, mea_id: int, sample_type: str):
    """§21 auto-batch: at ingest, group by folder_name.
    - Create the batch if missing (label = folder_name).
    - Link this measurement (measurement.batch_id).
    - If this measurement is a 'blank' or 'standard' and the batch has no
      pointer set for that role yet, set it. Existing pointers stay put
      (admin can re-designate manually).
    """
    cur.execute("SELECT batch_id, std_mea_id, blank_mea_id FROM batch WHERE label = %s",
                (folder_name,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO batch (label) VALUES (%s)", (folder_name,))
        batch_id = cur.lastrowid
        cur_std = cur_blank = None
    else:
        batch_id, cur_std, cur_blank = row
    cur.execute("UPDATE measurement SET batch_id = %s WHERE mea_id = %s",
                (batch_id, mea_id))
    if sample_type == "standard" and cur_std is None:
        cur.execute("UPDATE batch SET std_mea_id = %s WHERE batch_id = %s",
                    (mea_id, batch_id))
    elif sample_type == "blank" and cur_blank is None:
        cur.execute("UPDATE batch SET blank_mea_id = %s WHERE batch_id = %s",
                    (mea_id, batch_id))


def _register_keys(cur, header, mea_id):
    """§5 step 4: registry upsert. sample_value truncated to VARCHAR(255)."""
    for k, v in header.items():
        cur.execute("""
            INSERT INTO header_key_registry (key_name, first_mea_id, sample_value, occurrences)
            VALUES (%s, %s, %s, 1)
            ON DUPLICATE KEY UPDATE occurrences = occurrences + 1
        """, (k[:128], mea_id, (v or "")[:255]))


def ingest_one(conn, path: Path):
    """Full pipeline. Returns (result, message, mea_id_or_None).
    result in {'ok','ok_with_warnings','duplicate','parse_error','failed'}."""
    warnings: list[str] = []
    file_name = path.name
    try:
        raw = path.read_bytes()
    except Exception as e:
        return "failed", f"read error: {e}", None
    file_hash = hashlib.sha256(raw).hexdigest()

    with conn.cursor() as cur:
        cur.execute("SELECT mea_id FROM measurement WHERE file_hash=%s", (file_hash,))
        row = cur.fetchone()
        if row:
            return "duplicate", f"identical to mea_id={row[0]}", row[0]

    try:
        header, matrix, meta = split_mea(raw)
    except MeaParseError as e:
        return "parse_error", f"split_mea: {e}", None

    p = promote(header)
    if not p["measured_at"]:
        return "parse_error", "Timestamp key missing or unparseable", None
    if not p["machine_serial"]:
        return "parse_error", "Machine serial key missing", None

    rip_idx, rip_signed, polarity, weak = find_rip(matrix)
    if weak:
        warnings.append(f"weak/atypical RIP at drift idx={rip_idx}, mean={rip_signed:.1f}")
    if p["sample_rate_khz"]:
        rip_drift_ms = float(rip_idx) / float(p["sample_rate_khz"])
    else:
        rip_drift_ms = None

    stype = sample_type_from_name(p["sample_name"])
    if p["chunk_averages"] and p["trig_repetition_ms"]:
        run_time_s = matrix.shape[0] * float(p["trig_repetition_ms"]) / 1000.0 * float(p["chunk_averages"])
    else:
        run_time_s = None

    tel_series = parse_telemetry(header)
    for series, values in tel_series:
        if series == "press_ambient" and values:
            # G.A.S. reports Pressure Ambient array in Pa (~99764 at 1 atm);
            # measurement.ambient_pressure_kpa is DECIMAL(7,3) kPa -> /1000.
            p["ambient_pressure_kpa"] = float(np.mean(values)) / 1000.0
            break

    sample_name = p["sample_name"] or path.stem

    try:
        with conn.cursor() as cur:
            instrument_id = _upsert_instrument(cur, p)
            method_id = _upsert_method(cur, p, warnings)

            cur.execute("""
                INSERT INTO measurement
                    (file_name, file_hash, file_size_bytes, measured_at,
                     instrument_id, method_id,
                     sample_name, sample_class, sample_type, status,
                     header_bytes, n_spectra, n_drift_points, matrix_dtype,
                     chunk_averages, sample_rate_khz, trig_repetition_ms, run_time_s,
                     ambient_pressure_kpa, polarity, rip_drift_index, rip_drift_ms,
                     header_json, full_path, folder_name)
                VALUES (%s,%s,%s,%s, %s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s)
            """, (
                file_name, file_hash, len(raw), p["measured_at"],
                instrument_id, method_id,
                sample_name, p["sample_class"], stype, p["status"],
                meta["header_bytes"], matrix.shape[0], matrix.shape[1], "int16le",
                p["chunk_averages"], p["sample_rate_khz"], p["trig_repetition_ms"], run_time_s,
                p["ambient_pressure_kpa"], polarity, rip_idx, rip_drift_ms,
                json.dumps(header, ensure_ascii=False),
                str(path.resolve()), path.parent.name,
            ))
            mea_id = cur.lastrowid

            _register_keys(cur, header, mea_id)

            _upsert_batch_and_link(cur, path.parent.name, mea_id, stype)

            if tel_series:
                rows = []
                for series, values in tel_series:
                    for i, v in enumerate(values):
                        rows.append((mea_id, series, i, float(v)))
                cur.executemany(
                    "INSERT INTO run_telemetry (mea_id, series, seq_no, value) VALUES (%s,%s,%s,%s)",
                    rows,
                )

            pooled, prev_rows, prev_cols = max_pool(matrix, POOL_RT, POOL_DT)
            rt_axis, dt_axis = make_axes(
                prev_rows, prev_cols, POOL_RT, POOL_DT,
                p["sample_rate_khz"], p["trig_repetition_ms"], p["chunk_averages"])
            npz_bytes = pack_npz(pooled, rt_axis, dt_axis)

            # heatmap_png / thumb_png / png_render_ver -> NULL; render_previews.py fills them.
            cur.execute("""
                INSERT INTO mea_preview
                    (mea_id, preview_npz,
                     prev_rows, prev_cols, pool_rt, pool_dt, pipeline_ver)
                VALUES (%s,%s, %s,%s,%s,%s,%s)
            """, (mea_id, npz_bytes,
                  prev_rows, prev_cols, POOL_RT, POOL_DT, PIPELINE_VER))

            compressed = zstd.ZstdCompressor(level=3).compress(raw)
            cur.execute("""
                INSERT INTO mea_file (mea_id, compression, orig_size, stored_size, content)
                VALUES (%s, 'zstd', %s, %s, %s)
            """, (mea_id, len(raw), len(compressed), compressed))

        conn.commit()
        result = "ok_with_warnings" if warnings else "ok"
        return result, ("; ".join(warnings) if warnings else None), mea_id

    except pymysql.err.IntegrityError as e:
        conn.rollback()
        if "uk_hash" in str(e):
            return "duplicate", "race: uk_hash collision", None
        return "failed", f"IntegrityError: {e}", None
    except Exception as e:
        conn.rollback()
        return "failed", f"{type(e).__name__}: {e}", None


def _log(conn, mea_id, file_name, result, message):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingest_log (mea_id, file_name, result, message, pipeline_ver)
            VALUES (%s, %s, %s, %s, %s)
        """, (mea_id, file_name, result, (message or "")[:512], PIPELINE_VER))
    conn.commit()


def _find_files(root: Path):
    """Both .mea and .s.mea per §2e. Dedupe (rglob('*.mea') already matches '*.s.mea')."""
    seen, out = set(), []
    for f in sorted(root.rglob("*.mea")):
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description="Batch-ingest .mea files into gc-ims_database.")
    ap.add_argument("root", nargs="?", default="mea data", help="folder to scan recursively")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="shengic")
    ap.add_argument("--password", default="sirirat")
    ap.add_argument("--database", default="gc-ims_database")
    ap.add_argument("--dry-run", action="store_true", help="list files, do not ingest")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        sys.exit(f"folder not found: {root}")

    files = _find_files(root)
    print(f"found {len(files)} .mea files under {root}/")
    if args.dry_run:
        for f in files:
            print(f"  {f.relative_to(root)}  ({f.stat().st_size:,} B)")
        return

    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user, password=args.password,
        database=args.database, charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
    )

    counts = {}
    t0 = time.perf_counter()
    for i, f in enumerate(files, 1):
        rel = f.relative_to(root)
        t_start = time.perf_counter()
        result, msg, mea_id = ingest_one(conn, f)
        elapsed = time.perf_counter() - t_start
        _log(conn, mea_id, f.name, result, msg)
        counts[result] = counts.get(result, 0) + 1
        line = f"[{i:2d}/{len(files)}] {result:17s} {elapsed:5.1f}s  {rel}"
        if msg:
            line += f"  ({msg})"
        # ASCII-safe fallback for terminals that can't render Chinese in filenames
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode())

    total = time.perf_counter() - t0
    print("\n" + "=" * 60)
    print(f"SUMMARY  ({total:.1f} s total)")
    for k in ("ok", "ok_with_warnings", "duplicate", "parse_error", "failed"):
        if counts.get(k):
            print(f"  {k}: {counts[k]}")
    conn.close()


if __name__ == "__main__":
    main()
