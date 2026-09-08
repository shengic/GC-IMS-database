"""Soft-delete policy per DESIGN §15. Version 1.0.

Measurements are NEVER physically deleted — retired=1 marks them dead.
Search/list queries must default to `WHERE retired = 0`; admin UI has
a toggle. Retire/unretire is audit-logged like any admin action.
"""
import pytest


pytestmark = pytest.mark.testdb


def _seed_prereqs(cur):
    cur.execute("""INSERT INTO instrument (machine_type, machine_serial)
                   VALUES ('TEST', 'SN-2')""")
    inst_id = cur.lastrowid
    cur.execute("""INSERT INTO gc_method (program_name, program_raw, program_hash)
                   VALUES ('TEST', 'Name=`TEST`', REPEAT('b', 64))""")
    return inst_id, cur.lastrowid


def _insert_measurement(cur, file_hash, inst_id, method_id, retired=0,
                        retired_at=None, retired_by=None, retired_reason=None):
    # geometry satisfies ck_meas_geometry: 300 = 100 + 10*10*2
    # retired-flag consistency handled by ck_meas_retired_consistency: when
    # retired=1 the caller must also supply retired_at and retired_by.
    cur.execute("""
        INSERT INTO measurement
            (file_name, file_hash, file_size_bytes, measured_at,
             instrument_id, method_id, sample_name,
             header_bytes, n_spectra, n_drift_points, header_json,
             retired, retired_at, retired_by, retired_reason)
        VALUES ('x.mea', %s, 300, NOW(), %s, %s, 'X',
                100, 10, 10, JSON_OBJECT(),
                %s, %s, %s, %s)
    """, (file_hash, inst_id, method_id,
          retired, retired_at, retired_by, retired_reason))
    return cur.lastrowid


class TestRetiredFilter:
    def test_default_query_hides_retired(self, test_db_conn):
        import datetime as dt
        with test_db_conn.cursor() as c:
            inst, meth = _seed_prereqs(c)
            live = _insert_measurement(c, "a" * 64, inst, meth, retired=0)
            _insert_measurement(c, "b" * 64, inst, meth, retired=1,
                                retired_at=dt.datetime.now(), retired_by="tester")
            c.execute("SELECT mea_id FROM measurement WHERE retired = 0")
            ids = {r[0] for r in c.fetchall()}
            assert ids == {live}

    def test_toggle_shows_all(self, test_db_conn):
        import datetime as dt
        with test_db_conn.cursor() as c:
            inst, meth = _seed_prereqs(c)
            _insert_measurement(c, "a" * 64, inst, meth, retired=0)
            _insert_measurement(c, "b" * 64, inst, meth, retired=1,
                                retired_at=dt.datetime.now(), retired_by="tester")
            c.execute("SELECT COUNT(*) FROM measurement")
            assert c.fetchone()[0] == 2


class TestRetireMetadata:
    def test_retiring_sets_all_provenance_fields(self, test_db_conn):
        with test_db_conn.cursor() as c:
            inst, meth = _seed_prereqs(c)
            mid = _insert_measurement(c, "a" * 64, inst, meth)
            c.execute("""UPDATE measurement
                         SET retired = 1, retired_at = NOW(),
                             retired_by = %s, retired_reason = %s
                         WHERE mea_id = %s""",
                      ("shengic", "duplicate of mea_id=99", mid))
            c.execute("""SELECT retired, retired_by, retired_reason,
                                retired_at IS NOT NULL
                         FROM measurement WHERE mea_id = %s""", (mid,))
            r, by, reason, at_set = c.fetchone()
            assert r == 1
            assert by == "shengic"
            assert reason == "duplicate of mea_id=99"
            assert at_set == 1

    def test_hash_still_blocks_reimport_after_retire(self, test_db_conn):
        """§15: retiring does not free the file_hash — the record is IN
        the library, just retired. Re-importing must fail."""
        import datetime as dt
        import pymysql
        with test_db_conn.cursor() as c:
            inst, meth = _seed_prereqs(c)
            _insert_measurement(c, "a" * 64, inst, meth, retired=1,
                                retired_at=dt.datetime.now(), retired_by="tester")
            with pytest.raises(pymysql.err.IntegrityError, match="uk_hash"):
                _insert_measurement(c, "a" * 64, inst, meth, retired=0)
