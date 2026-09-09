"""Full-scan integrity audit against the LIVE `gc-ims_database`. Version 1.1.

Every test is read-only and asserts an invariant that must hold for
correctly-ingested data. Run periodically (post-batch, nightly, after
recipe changes) to detect drift. If a test here fails, the DB is
INCONSISTENT — not just the code — and needs investigation.

Grouped:
  - referential  : row counts, FK completeness
  - geometry     : byte-count math, RIP index bounds, ENUM validity
  - content      : SHA-256 roundtrip via zstd (DESIGN §14 retrieval invariant)
  - preview      : npz load, shape match, PNG validity
  - business     : retired-flag consistency, sample_type ENUM, ambient
                   pressure sanity
  - telemetry    : contiguous seq_no per (mea_id, series)
  - audit        : ingest_log coverage, header_key_registry completeness
  - dedup        : no duplicate file_hash

Marked `@pytest.mark.db`; run with plain `pytest` (has `-m "not db"` opt-out).
Some tests marked `slow` — the SHA-256 scan decompresses every blob.
"""
import hashlib
import io
import json

import numpy as np
import pytest
import zstandard as zstd
from PIL import Image

pytestmark = pytest.mark.db


# ---------- referential ----------

class TestReferentialConsistency:
    def test_no_orphan_mea_file_rows(self, db_conn):
        """§11 permits measurement without mea_file (tiered demo). The reverse
        is a corruption: an mea_file blob whose measurement was deleted."""
        with db_conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FROM mea_file f
                         LEFT JOIN measurement m ON m.mea_id = f.mea_id
                         WHERE m.mea_id IS NULL""")
            assert c.fetchone()[0] == 0

    def test_every_measurement_has_a_preview(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT m.mea_id FROM measurement m
                         LEFT JOIN mea_preview p ON p.mea_id = m.mea_id
                         WHERE p.mea_id IS NULL""")
            missing = [r[0] for r in c.fetchall()]
            assert not missing, f"measurements without preview: {missing}"

    def test_no_orphan_preview_rows(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FROM mea_preview p
                         LEFT JOIN measurement m ON m.mea_id = p.mea_id
                         WHERE m.mea_id IS NULL""")
            assert c.fetchone()[0] == 0

    def test_no_orphan_telemetry_rows(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FROM run_telemetry t
                         LEFT JOIN measurement m ON m.mea_id = t.mea_id
                         WHERE m.mea_id IS NULL""")
            assert c.fetchone()[0] == 0


# ---------- geometry ----------

class TestGeometryConsistency:
    def test_matrix_byte_count_equals_file_minus_header(self, db_conn):
        """DESIGN §2b / §5: len(raw) - header_bytes == n_spectra * n_drift_points * 2.
        Byte-exact reshape depends on this equality."""
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, file_size_bytes, header_bytes,
                                n_spectra, n_drift_points
                         FROM measurement""")
            for mid, size, hdr, ns, nd in c.fetchall():
                expected = ns * nd * 2
                actual = size - hdr
                assert expected == actual, (
                    f"mea_id={mid}: matrix bytes {actual} != {expected} "
                    f"({ns} spectra x {nd} drift pts x 2)")

    def test_polarity_only_positive_or_negative(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT DISTINCT polarity FROM measurement
                         WHERE polarity IS NOT NULL""")
            values = {r[0] for r in c.fetchall()}
            assert values.issubset({"positive", "negative"}), values

    def test_sample_type_within_enum(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("SELECT DISTINCT sample_type FROM measurement")
            values = {r[0] for r in c.fetchall()}
            assert values.issubset(
                {"sample", "blank", "standard", "qc", "unknown"}), values

    def test_rip_drift_index_within_axis(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, rip_drift_index, n_drift_points
                         FROM measurement WHERE rip_drift_index IS NOT NULL""")
            for mid, idx, n in c.fetchall():
                assert 0 <= idx < n, f"mea_id={mid}: RIP idx {idx} out of [0,{n})"

    def test_matrix_dtype_is_declared(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("SELECT DISTINCT matrix_dtype FROM measurement")
            values = {r[0] for r in c.fetchall()}
            assert values.issubset({"int16le"}), values  # only supported dtype today


# ---------- content: SHA-256 roundtrip ----------

class TestContentIntegrity:
    """DESIGN §14: verify SHA-256 on retrieval — decompress mea_file.content,
    hash the raw bytes, must equal measurement.file_hash. The regulatory /
    data-integrity guarantee. Slow: touches every stored blob."""

    @pytest.mark.slow
    def test_every_mea_file_hash_matches(self, db_conn):
        # Stream row-by-row to avoid pulling every blob into memory at once.
        with db_conn.cursor() as c:
            c.execute("SELECT mea_id FROM measurement ORDER BY mea_id")
            ids = [r[0] for r in c.fetchall()]

        dctx = zstd.ZstdDecompressor()
        mismatches = []
        for mid in ids:
            with db_conn.cursor() as c:
                c.execute("""SELECT m.file_hash, m.file_size_bytes,
                                    f.compression, f.orig_size, f.content
                             FROM measurement m
                             JOIN mea_file f ON f.mea_id = m.mea_id
                             WHERE m.mea_id = %s""", (mid,))
                row = c.fetchone()
            if row is None:
                continue  # measurement without mea_file (§11 tiered demo)
            expected_hash, meas_size, comp, orig_size, blob = row
            if comp == "zstd":
                raw = dctx.decompress(blob, max_output_size=orig_size)
            elif comp == "none":
                raw = blob
            else:
                mismatches.append(f"mea_id={mid}: unknown compression={comp!r}")
                continue
            if len(raw) != orig_size:
                mismatches.append(
                    f"mea_id={mid}: decompressed {len(raw)} vs orig_size {orig_size}")
            if len(raw) != meas_size:
                mismatches.append(
                    f"mea_id={mid}: raw {len(raw)} vs measurement.file_size_bytes {meas_size}")
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected_hash:
                mismatches.append(
                    f"mea_id={mid}: sha256 mismatch (got {actual[:16]}..., "
                    f"expected {expected_hash[:16]}...)")
        assert not mismatches, "\n".join(mismatches)


# ---------- preview ----------

class TestPreviewIntegrity:
    def test_npz_loads_and_matrix_shape_matches(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, preview_npz, prev_rows, prev_cols
                         FROM mea_preview""")
            rows = c.fetchall()
        bad = []
        for mid, blob, rows_, cols_ in rows:
            try:
                with np.load(io.BytesIO(blob)) as z:
                    if "matrix" not in z.files:
                        bad.append(f"mea_id={mid}: npz missing 'matrix'")
                        continue
                    if z["matrix"].shape != (rows_, cols_):
                        bad.append(f"mea_id={mid}: matrix {z['matrix'].shape} != "
                                   f"({rows_}, {cols_})")
            except Exception as e:
                bad.append(f"mea_id={mid}: npz load failed: {e}")
        assert not bad, "\n".join(bad)

    def test_axes_length_matches_pooled_dims(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, preview_npz, prev_rows, prev_cols
                         FROM mea_preview""")
            rows = c.fetchall()
        bad = []
        for mid, blob, rows_, cols_ in rows:
            with np.load(io.BytesIO(blob)) as z:
                if "rt_axis_s" in z.files and len(z["rt_axis_s"]) != rows_:
                    bad.append(f"mea_id={mid}: rt_axis len {len(z['rt_axis_s'])} != {rows_}")
                if "dt_axis_ms" in z.files and len(z["dt_axis_ms"]) != cols_:
                    bad.append(f"mea_id={mid}: dt_axis len {len(z['dt_axis_ms'])} != {cols_}")
        assert not bad, "\n".join(bad)

    def test_pool_factors_derive_pooled_dims(self, db_conn):
        """prev_rows = n_spectra // pool_rt (floor div). Same for cols."""
        with db_conn.cursor() as c:
            c.execute("""SELECT m.mea_id, m.n_spectra, m.n_drift_points,
                                p.prev_rows, p.prev_cols, p.pool_rt, p.pool_dt
                         FROM mea_preview p JOIN measurement m USING (mea_id)""")
            for mid, ns, nd, pr, pc, prt, pdt in c.fetchall():
                assert pr == ns // prt, (
                    f"mea_id={mid}: prev_rows {pr} != {ns}//{prt}={ns//prt}")
                assert pc == nd // pdt, (
                    f"mea_id={mid}: prev_cols {pc} != {nd}//{pdt}={nd//pdt}")

    def test_heatmap_png_valid_when_present(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, heatmap_png, thumb_png
                         FROM mea_preview WHERE heatmap_png IS NOT NULL""")
            rows = c.fetchall()
        bad = []
        for mid, heat, thumb in rows:
            if not heat.startswith(b"\x89PNG\r\n\x1a\n"):
                bad.append(f"mea_id={mid}: heatmap not a PNG")
                continue
            try:
                Image.open(io.BytesIO(heat)).verify()
            except Exception as e:
                bad.append(f"mea_id={mid}: heatmap PIL.verify failed: {e}")
            if thumb is not None:
                if not thumb.startswith(b"\x89PNG\r\n\x1a\n"):
                    bad.append(f"mea_id={mid}: thumb not a PNG")
                else:
                    try:
                        Image.open(io.BytesIO(thumb)).verify()
                    except Exception as e:
                        bad.append(f"mea_id={mid}: thumb PIL.verify failed: {e}")
        assert not bad, "\n".join(bad)

    def test_render_ver_present_when_pngs_present(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FROM mea_preview
                         WHERE (heatmap_png IS NOT NULL OR thumb_png IS NOT NULL)
                           AND png_render_ver IS NULL""")
            assert c.fetchone()[0] == 0


# ---------- business rules ----------

class TestBusinessRuleConsistency:
    def test_retired_zero_has_no_retire_metadata(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FROM measurement
                         WHERE retired = 0
                           AND (retired_at IS NOT NULL
                                OR retired_by IS NOT NULL
                                OR retired_reason IS NOT NULL)""")
            assert c.fetchone()[0] == 0

    def test_retired_one_has_at_and_by(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT COUNT(*) FROM measurement
                         WHERE retired = 1
                           AND (retired_at IS NULL OR retired_by IS NULL)""")
            assert c.fetchone()[0] == 0

    def test_ambient_pressure_kpa_atmospheric_range(self, db_conn):
        """Regression against the Pa/kPa unit bug: atmospheric pressure
        anywhere on Earth is ~85-108 kPa. Values outside 70-115 are
        almost certainly a unit mistake (Pa stored as kPa)."""
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, ambient_pressure_kpa FROM measurement
                         WHERE ambient_pressure_kpa IS NOT NULL
                           AND (ambient_pressure_kpa < 70
                                OR ambient_pressure_kpa > 115)""")
            offenders = c.fetchall()
            assert not offenders, (
                f"ambient_pressure_kpa outside atmospheric range: {offenders}")

    def test_run_time_matches_geometry_within_tolerance(self, db_conn):
        """run_time_s ≈ n_spectra * trig_repetition_ms/1000 * chunk_averages.
        Tolerance: 1% (rounding in the DECIMAL storage)."""
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, run_time_s, n_spectra,
                                trig_repetition_ms, chunk_averages
                         FROM measurement
                         WHERE run_time_s IS NOT NULL
                           AND trig_repetition_ms IS NOT NULL
                           AND chunk_averages IS NOT NULL""")
            bad = []
            for mid, rt, ns, trig, avg in c.fetchall():
                expected = float(ns) * float(trig) / 1000.0 * float(avg)
                if abs(float(rt) - expected) > max(0.01 * expected, 1.0):
                    bad.append(f"mea_id={mid}: run_time_s {rt} != expected {expected:.1f}")
            assert not bad, "\n".join(bad)


# ---------- telemetry ----------

class TestTelemetryConsistency:
    def test_seq_no_contiguous_per_series(self, db_conn):
        """Per (mea_id, series): seq_no is 0..count-1 with no gaps."""
        with db_conn.cursor() as c:
            c.execute("""SELECT mea_id, series, MIN(seq_no), MAX(seq_no), COUNT(*)
                         FROM run_telemetry
                         GROUP BY mea_id, series""")
            bad = []
            for mid, series, lo, hi, n in c.fetchall():
                if lo != 0 or hi != n - 1:
                    bad.append(f"mea_id={mid} series={series}: "
                               f"seq_no range [{lo}, {hi}] with count={n}")
            assert not bad, "\n".join(bad)

    def test_series_values_within_enum(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("SELECT DISTINCT series FROM run_telemetry")
            found = {r[0] for r in c.fetchall()}
            allowed = {"flow_ims", "flow_gc", "press_ims", "press_gc",
                       "press_ambient", "pump1_flow", "pump1_pressure"}
            assert found.issubset(allowed), found - allowed


# ---------- audit trail ----------

class TestAuditTrailConsistency:
    def test_every_measurement_has_ok_ingest_log(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT m.mea_id FROM measurement m
                         WHERE NOT EXISTS (
                             SELECT 1 FROM ingest_log l
                             WHERE l.mea_id = m.mea_id
                               AND l.result IN ('ok', 'ok_with_warnings')
                         )""")
            missing = [r[0] for r in c.fetchall()]
            assert not missing, (
                f"measurements without ok ingest_log: {missing[:10]}"
                + ("..." if len(missing) > 10 else ""))

    def test_ingest_log_mea_ids_are_valid_when_set(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT l.log_id, l.mea_id, l.result
                         FROM ingest_log l
                         LEFT JOIN measurement m ON m.mea_id = l.mea_id
                         WHERE l.mea_id IS NOT NULL
                           AND m.mea_id IS NULL""")
            orphans = c.fetchall()
            assert not orphans, f"ingest_log rows with dangling mea_id: {orphans}"


class TestHeaderRegistryCompleteness:
    def test_every_key_in_header_json_is_registered(self, db_conn):
        """DESIGN §5 step 4: registry is an incrementally maintained union
        of keys ever seen. Every distinct key in any header_json must have
        a row in header_key_registry."""
        with db_conn.cursor() as c:
            c.execute("SELECT header_json FROM measurement")
            seen = set()
            for (hj,) in c.fetchall():
                if hj is None:
                    continue
                d = json.loads(hj) if isinstance(hj, str) else hj
                seen.update(d.keys())
            c.execute("SELECT key_name FROM header_key_registry")
            registered = {r[0] for r in c.fetchall()}
        missing = seen - registered
        assert not missing, f"unregistered header keys: {sorted(missing)[:20]}"


# ---------- batch (§21) ----------

class TestBatchConsistency:
    def test_batch_std_points_at_standard(self, db_conn):
        """A batch's std_mea_id must reference a measurement with
        sample_type='standard' (else the auto-classifier or batch admin
        made a mistake)."""
        with db_conn.cursor() as c:
            c.execute("""SELECT b.batch_id, b.label, m.mea_id, m.sample_type
                         FROM batch b JOIN measurement m ON m.mea_id = b.std_mea_id
                         WHERE b.std_mea_id IS NOT NULL
                           AND m.sample_type != 'standard'""")
            bad = c.fetchall()
            assert not bad, f"batches with std_mea_id pointing at non-standard: {bad}"

    def test_batch_blank_points_at_blank(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT b.batch_id, b.label, m.mea_id, m.sample_type
                         FROM batch b JOIN measurement m ON m.mea_id = b.blank_mea_id
                         WHERE b.blank_mea_id IS NOT NULL
                           AND m.sample_type != 'blank'""")
            bad = c.fetchall()
            assert not bad, f"batches with blank_mea_id pointing at non-blank: {bad}"

    def test_std_and_blank_belong_to_same_batch(self, db_conn):
        """A batch's calibration files should themselves be members of that
        batch (their measurement.batch_id points back to the batch)."""
        with db_conn.cursor() as c:
            c.execute("""SELECT b.batch_id, b.label, b.std_mea_id, m.batch_id
                         FROM batch b JOIN measurement m ON m.mea_id = b.std_mea_id
                         WHERE b.std_mea_id IS NOT NULL
                           AND (m.batch_id IS NULL OR m.batch_id != b.batch_id)""")
            bad = c.fetchall()
            assert not bad, f"std files not linked back to their batch: {bad}"
            c.execute("""SELECT b.batch_id, b.label, b.blank_mea_id, m.batch_id
                         FROM batch b JOIN measurement m ON m.mea_id = b.blank_mea_id
                         WHERE b.blank_mea_id IS NOT NULL
                           AND (m.batch_id IS NULL OR m.batch_id != b.batch_id)""")
            bad = c.fetchall()
            assert not bad, f"blank files not linked back to their batch: {bad}"


# ---------- dedup ----------

class TestDedupInvariant:
    def test_no_duplicate_file_hashes(self, db_conn):
        """Belt-and-suspenders for uk_hash: production data must obey."""
        with db_conn.cursor() as c:
            c.execute("""SELECT file_hash, COUNT(*)
                         FROM measurement
                         GROUP BY file_hash HAVING COUNT(*) > 1""")
            dups = c.fetchall()
            assert not dups, f"duplicate file_hashes: {dups}"

    def test_program_hash_uniqueness(self, db_conn):
        """gc_method.program_hash is uk_program: same recipe → same row."""
        with db_conn.cursor() as c:
            c.execute("""SELECT program_hash, COUNT(*)
                         FROM gc_method
                         GROUP BY program_hash HAVING COUNT(*) > 1""")
            dups = c.fetchall()
            assert not dups, f"duplicate program_hashes: {dups}"

    def test_instrument_serial_uniqueness(self, db_conn):
        with db_conn.cursor() as c:
            c.execute("""SELECT machine_serial, COUNT(*)
                         FROM instrument
                         GROUP BY machine_serial HAVING COUNT(*) > 1""")
            dups = c.fetchall()
            assert not dups, f"duplicate machine_serials: {dups}"
