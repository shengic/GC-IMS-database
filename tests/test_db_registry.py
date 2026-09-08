"""header_key_registry upsert semantics per DESIGN §5 step 4. Version 1.0.

Every ingested key runs INSERT ... ON DUPLICATE KEY UPDATE
occurrences = occurrences + 1. The registry is thus an incrementally
maintained union, never re-scanned from header_json at read time.
"""
import pytest


pytestmark = pytest.mark.testdb


class TestRegistryUpsert:
    def test_first_insert_creates_row_with_occurrences_1(self, test_db_conn):
        with test_db_conn.cursor() as c:
            c.execute("""
                INSERT INTO header_key_registry
                    (key_name, first_mea_id, sample_value, occurrences)
                VALUES (%s, NULL, %s, 1)
                ON DUPLICATE KEY UPDATE occurrences = occurrences + 1
            """, ("Filter", "SG8"))
            c.execute("SELECT occurrences, sample_value FROM header_key_registry "
                      "WHERE key_name = %s", ("Filter",))
            occ, val = c.fetchone()
            assert occ == 1
            assert val == "SG8"

    def test_second_insert_increments_occurrences(self, test_db_conn):
        with test_db_conn.cursor() as c:
            for _ in range(3):
                c.execute("""
                    INSERT INTO header_key_registry
                        (key_name, first_mea_id, sample_value, occurrences)
                    VALUES (%s, NULL, %s, 1)
                    ON DUPLICATE KEY UPDATE occurrences = occurrences + 1
                """, ("Filter", "SG8"))
            c.execute("SELECT occurrences FROM header_key_registry "
                      "WHERE key_name = %s", ("Filter",))
            assert c.fetchone()[0] == 3

    def test_first_seen_at_preserved_across_updates(self, test_db_conn):
        with test_db_conn.cursor() as c:
            c.execute("""
                INSERT INTO header_key_registry
                    (key_name, first_mea_id, sample_value, occurrences)
                VALUES (%s, NULL, %s, 1)
                ON DUPLICATE KEY UPDATE occurrences = occurrences + 1
            """, ("Filter", "first"))
            c.execute("SELECT first_seen_at FROM header_key_registry "
                      "WHERE key_name = %s", ("Filter",))
            first_seen = c.fetchone()[0]
            # second upsert — first_seen_at must not change
            c.execute("""
                INSERT INTO header_key_registry
                    (key_name, first_mea_id, sample_value, occurrences)
                VALUES (%s, NULL, %s, 1)
                ON DUPLICATE KEY UPDATE occurrences = occurrences + 1
            """, ("Filter", "second"))
            c.execute("SELECT first_seen_at FROM header_key_registry "
                      "WHERE key_name = %s", ("Filter",))
            assert c.fetchone()[0] == first_seen
