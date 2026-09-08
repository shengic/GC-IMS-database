"""Duplicate-import protection per DESIGN §14. Version 1.0.

file_hash UNIQUE constraint (`uk_hash`) is the race-proof backstop — a
concurrent INSERT that beats the app's check-then-insert cannot
double-store the same content.
"""
import pytest
import pymysql


pytestmark = pytest.mark.testdb


def _seed_prereqs(cur):
    """Insert an instrument + gc_method so measurement FKs can resolve."""
    cur.execute("""
        INSERT INTO instrument (machine_type, machine_serial)
        VALUES ('TEST', 'SN-1')
    """)
    inst_id = cur.lastrowid
    cur.execute("""
        INSERT INTO gc_method (program_name, program_raw, program_hash)
        VALUES ('TEST', 'Name=`TEST`', REPEAT('a', 64))
    """)
    method_id = cur.lastrowid
    return inst_id, method_id


def _insert_measurement(cur, file_hash, inst_id, method_id,
                        file_name="x.mea", sample="Sample-X"):
    # geometry satisfies ck_meas_geometry: 300 = 100 + 10*10*2
    cur.execute("""
        INSERT INTO measurement
            (file_name, file_hash, file_size_bytes, measured_at,
             instrument_id, method_id, sample_name,
             header_bytes, n_spectra, n_drift_points, header_json)
        VALUES (%s, %s, 300, NOW(), %s, %s, %s, 100, 10, 10, JSON_OBJECT())
    """, (file_name, file_hash, inst_id, method_id, sample))
    return cur.lastrowid


class TestFileHashUnique:
    def test_two_inserts_with_same_hash_second_fails(self, test_db_conn):
        h = "a" * 64
        with test_db_conn.cursor() as c:
            inst, meth = _seed_prereqs(c)
            _insert_measurement(c, h, inst, meth, file_name="orig.mea")
            with pytest.raises(pymysql.err.IntegrityError, match="uk_hash"):
                _insert_measurement(c, h, inst, meth, file_name="dup.mea")

    def test_different_hashes_both_accepted(self, test_db_conn):
        with test_db_conn.cursor() as c:
            inst, meth = _seed_prereqs(c)
            _insert_measurement(c, "a" * 64, inst, meth)
            _insert_measurement(c, "b" * 64, inst, meth)
            c.execute("SELECT COUNT(*) FROM measurement")
            assert c.fetchone()[0] == 2
