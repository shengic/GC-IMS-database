"""CHECK constraints on `measurement` reject bad data at INSERT time. Version 1.1.

Companion to test_integrity_scan.py: those tests find bad data that
somehow made it in; these tests verify the DB WON'T LET IT IN.

Every test attempts an INSERT that violates one CHECK; the DB must
raise IntegrityError (SQLSTATE 3819 in MySQL 8, mapped by pymysql).
"""
import pytest
import pymysql


pytestmark = pytest.mark.testdb


def _seed(cur):
    cur.execute("""INSERT INTO instrument (machine_type, machine_serial)
                   VALUES ('TEST', 'SN-CK')""")
    inst = cur.lastrowid
    cur.execute("""INSERT INTO gc_method (program_name, program_raw, program_hash)
                   VALUES ('TEST', 'Name=`T`', REPEAT('c', 64))""")
    return inst, cur.lastrowid


def _insert(cur, **overrides):
    """Insert a valid measurement row; caller may override any field."""
    inst_id = overrides.pop("_inst", None)
    method_id = overrides.pop("_method", None)
    if inst_id is None or method_id is None:
        inst_id, method_id = _seed(cur)
    defaults = dict(
        file_name="ok.mea",
        file_hash="a" * 64,
        # geometry: 100 + 10*10*2 = 300 bytes
        file_size_bytes=300,
        header_bytes=100,
        n_spectra=10,
        n_drift_points=10,
        matrix_dtype="int16le",
        rip_drift_index=None,
        retired=0,
        retired_at=None,
        retired_by=None,
        retired_reason=None,
    )
    defaults.update(overrides)
    cur.execute(f"""
        INSERT INTO measurement
            (file_name, file_hash, file_size_bytes, measured_at, instrument_id, method_id,
             sample_name, header_bytes, n_spectra, n_drift_points, matrix_dtype,
             rip_drift_index, header_json,
             retired, retired_at, retired_by, retired_reason)
        VALUES (%s, %s, %s, NOW(), %s, %s,
                'X', %s, %s, %s, %s,
                %s, JSON_OBJECT(),
                %s, %s, %s, %s)
    """, (
        defaults["file_name"], defaults["file_hash"], defaults["file_size_bytes"],
        inst_id, method_id,
        defaults["header_bytes"], defaults["n_spectra"], defaults["n_drift_points"],
        defaults["matrix_dtype"],
        defaults["rip_drift_index"],
        defaults["retired"], defaults["retired_at"],
        defaults["retired_by"], defaults["retired_reason"],
    ))


class TestGeometryConstraint:
    """ck_meas_geometry: file_size = header_bytes + n_spectra * n_drift_points * 2"""

    def test_baseline_row_accepted(self, test_db_conn):
        with test_db_conn.cursor() as c:
            _insert(c)  # 300 == 100 + 10*10*2 — accepted

    def test_wrong_file_size_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError, match="ck_meas_geometry"):
                _insert(c, file_size_bytes=301)

    def test_wrong_header_bytes_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError, match="ck_meas_geometry"):
                _insert(c, header_bytes=101)  # 300 != 101 + 200

    def test_wrong_geometry_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError, match="ck_meas_geometry"):
                _insert(c, n_spectra=11)  # 300 != 100 + 11*10*2


class TestRetiredConsistencyConstraint:
    """ck_meas_retired_consistency: retired=0 has nothing set;
    retired=1 needs retired_at AND retired_by (retired_reason optional)."""

    def test_default_live_row_accepted(self, test_db_conn):
        with test_db_conn.cursor() as c:
            _insert(c)  # retired=0, all NULL — accepted

    def test_retired_with_at_and_by_accepted(self, test_db_conn):
        import datetime as dt
        with test_db_conn.cursor() as c:
            _insert(c, retired=1, retired_at=dt.datetime.now(),
                    retired_by="tester", retired_reason=None)

    def test_retired_zero_but_retired_at_set_rejected(self, test_db_conn):
        import datetime as dt
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_retired_consistency"):
                _insert(c, retired=0, retired_at=dt.datetime.now())

    def test_retired_one_missing_at_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_retired_consistency"):
                _insert(c, retired=1, retired_at=None, retired_by="tester")

    def test_retired_one_missing_by_rejected(self, test_db_conn):
        import datetime as dt
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_retired_consistency"):
                _insert(c, retired=1, retired_at=dt.datetime.now(), retired_by=None)


class TestRipIndexConstraint:
    """ck_meas_rip_index_within_axis: NULL OR < n_drift_points.
    Note: INT UNSIGNED already prevents negative values."""

    def test_null_rip_index_accepted(self, test_db_conn):
        with test_db_conn.cursor() as c:
            _insert(c, rip_drift_index=None)

    def test_rip_within_range_accepted(self, test_db_conn):
        with test_db_conn.cursor() as c:
            _insert(c, rip_drift_index=5)  # n_drift_points=10, so 5 is valid

    def test_rip_equal_to_n_drift_points_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_rip_index_within_axis"):
                _insert(c, rip_drift_index=10)  # off-by-one

    def test_rip_beyond_axis_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_rip_index_within_axis"):
                _insert(c, rip_drift_index=999)


class TestMatrixDtypeConstraint:
    """ck_meas_matrix_dtype: whitelisted values only (currently 'int16le').
    Future dtypes require widening this CHECK — deliberate friction so
    the codebase stays honest about which dtypes it actually supports."""

    def test_int16le_accepted(self, test_db_conn):
        with test_db_conn.cursor() as c:
            _insert(c, matrix_dtype="int16le")

    def test_unknown_dtype_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_matrix_dtype"):
                _insert(c, matrix_dtype="int32le")

    def test_empty_dtype_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            with pytest.raises(pymysql.err.OperationalError,
                               match="ck_meas_matrix_dtype"):
                _insert(c, matrix_dtype="")
