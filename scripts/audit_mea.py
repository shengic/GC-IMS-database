"""Dry-run audit of .mea files against the design-doc split_mea() logic.
Version 1.0. No DB writes. Reports geometry, header key set, polarity,
and any surprises that could stress the current schema."""
from __future__ import annotations
import re, sys, io
from pathlib import Path
import numpy as np

PRINTABLE = set(range(32, 256)) | {9, 10, 13}


class MeaParseError(Exception):
    pass


def split_mea(raw: bytes):
    head_txt = raw[:200_000].decode("latin-1", errors="replace")
    m1 = re.search(r"Chunks count\s*=\s*(\d+)", head_txt)
    m2 = re.search(r"Chunk sample count\s*=\s*(\d+)", head_txt)
    if not (m1 and m2):
        raise MeaParseError("geometry keys not found in leading text")
    n_spec, n_drift = int(m1.group(1)), int(m2.group(1))
    boundary = len(raw) - n_spec * n_drift * 2
    if boundary <= 0:
        raise MeaParseError(f"declared matrix larger than file: boundary={boundary}")

    # heuristic cross-check
    heur = next((i for i, b in enumerate(raw[:300_000]) if b not in PRINTABLE), -1)
    if heur < 0:
        raise MeaParseError("no non-printable byte found in first 300kB")
    delta = boundary - (heur + 1)
    if abs(delta) > 4:
        raise MeaParseError(f"boundary mismatch: arith={boundary} heuristic={heur} delta={delta}")

    header = {}
    for line in raw[:boundary].decode("latin-1").split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            header[k.strip()] = v.strip()

    matrix = np.frombuffer(raw[boundary:], dtype="<i2").reshape(n_spec, n_drift)
    return header, matrix, {"boundary": boundary, "heur_first_np": heur, "delta": delta}


def rip_polarity(matrix: np.ndarray):
    col_mean = matrix.mean(axis=0)
    idx = int(np.argmax(np.abs(col_mean)))
    signed = float(col_mean[idx])
    return ("negative" if signed < 0 else "positive"), idx, signed


def audit(path: Path):
    raw = path.read_bytes()
    try:
        header, matrix, meta = split_mea(raw)
    except MeaParseError as e:
        return {"path": path, "error": str(e), "size": len(raw)}
    pol, rip_idx, rip_val = rip_polarity(matrix)
    return {
        "path": path,
        "size": len(raw),
        "n_keys": len(header),
        "boundary": meta["boundary"],
        "delta": meta["delta"],
        "shape": matrix.shape,
        "polarity": pol,
        "rip_idx": rip_idx,
        "rip_val": rip_val,
        "firmware": header.get("Firmware version") or header.get("Firmware Version"),
        "machine_type": header.get("Machine type"),
        "machine_serial": header.get("Machine serial"),
        "timestamp": header.get("Timestamp"),
        "sample": header.get("Sample"),
        "program_name": header.get("Program"),
        "keys": frozenset(header.keys()),
        "raw_header": header,
    }


def main():
    root = Path("mea data")
    # one representative per subfolder + the two 5F1-00554 files (different sizes) + a demo-ketone .s.mea
    picks = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        files = sorted(sub.rglob("*.mea"))
        if not files:
            continue
        # dedupe by size — usually one per unique geometry
        seen_sizes = set()
        for f in files:
            if f.stat().st_size not in seen_sizes:
                picks.append(f)
                seen_sizes.add(f.stat().st_size)

    print(f"auditing {len(picks)} representative files\n")
    results = []
    for f in picks:
        print(f"--- {f.relative_to(root)}  ({f.stat().st_size:,} B) ---")
        r = audit(f)
        if "error" in r:
            print(f"  PARSE ERROR: {r['error']}")
            continue
        results.append(r)
        print(f"  firmware={r['firmware']}  machine={r['machine_type']}  serial={r['machine_serial']}")
        print(f"  boundary={r['boundary']}  delta={r['delta']}  keys={r['n_keys']}  shape={r['shape']}")
        print(f"  polarity={r['polarity']}  rip_idx={r['rip_idx']}  rip_signed_mean={r['rip_val']:.1f}")
        print(f"  sample={r['sample']!r}  program={r['program_name']!r}  ts={r['timestamp']}")
        print()

    # cross-file key comparison
    print("=" * 70)
    print("HEADER KEY UNION / DIFF ACROSS FILES")
    print("=" * 70)
    all_keys = set().union(*[r["keys"] for r in results])
    common = set.intersection(*[set(r["keys"]) for r in results]) if results else set()
    print(f"total distinct keys across all files: {len(all_keys)}")
    print(f"keys present in EVERY file: {len(common)}")
    print()
    print("keys UNIQUE to each file:")
    for r in results:
        others = set().union(*[o["keys"] for o in results if o is not r])
        unique = r["keys"] - others
        if unique:
            print(f"  {r['path'].name}:")
            for k in sorted(unique):
                v = r["raw_header"].get(k, "")[:40]
                print(f"    - {k!r} = {v!r}")

    # compare against DESIGN-doc validated set
    validated_keys_fw216 = 51
    validated_keys_fw252 = 60
    validated_keys_fw482 = 68
    print()
    print("compare to design-doc validated counts (fw2.16=51, 2.52=60, 4.82=68):")
    for r in results:
        fw = r["firmware"]
        n = r["n_keys"]
        print(f"  {r['path'].name}: fw={fw} keys={n}")

    # promoted-column key check (from schema)
    PROMOTED = {
        "Machine type", "Machine serial", "Machine name", "ADIO serial",
        "Firmware version", "Firmware Date",
        "nom Drift Tube Length", "nom Drift Potential Difference",
        "Sensor data",
        "Program", "GC Column", "Drift Gas",
        "Flow IMS setpoint", "Flow GC setpoint",
        "Flow1 setpoint", "Flow2 setpoint",
        "Timestamp", "Sample", "Class", "Status",
        "Chunks count", "Chunk sample count", "Chunk averages",
        "Sample rate", "Trig. repetition time",
        "EPC ambient pressure", "Start ambient pressure",
    }
    print()
    print("promoted-column keys ABSENT per file (schema tolerates via NULL):")
    for r in results:
        missing = PROMOTED - r["keys"]
        if missing:
            print(f"  {r['path'].name}: {sorted(missing)}")

    # telemetry series keys check
    TELEMETRY_KEYS = {
        "Flow Epc 1", "Flow Epc 2", "Pressure Epc 1", "Pressure Epc 2", "Pressure Ambient",  # fw<=2.x
        "Flow EPC IMS", "Flow EPC GC", "Pressure EPC IMS", "Pressure EPC GC",                # fw>=4.x
    }
    print()
    print("telemetry series present per file:")
    for r in results:
        present = TELEMETRY_KEYS & r["keys"]
        print(f"  {r['path'].name}: {sorted(present)}")

    # unknown/novel keys not in either PROMOTED or TELEMETRY (would go to header_json + registry)
    print()
    print("novel keys across ALL files (not promoted, not telemetry):")
    novel = all_keys - PROMOTED - TELEMETRY_KEYS
    for k in sorted(novel):
        # show which files carry it and one sample value
        carriers = [r for r in results if k in r["keys"]]
        sample_val = carriers[0]["raw_header"][k][:60] if carriers else ""
        print(f"  ({len(carriers)}/{len(results)}) {k!r} e.g. {sample_val!r}")


if __name__ == "__main__":
    main()
