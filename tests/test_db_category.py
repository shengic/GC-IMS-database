"""sample_category anti-cycle protections per DESIGN §3c. Version 1.1.

Three layers:
  (a) triggers block SELF-parenting only (MySQL 1442 prevents SELECT-on-same-
      table in a trigger; multi-hop cycles cannot be caught here);
  (b) move_category procedure runs a subtree CTE check — the ONLY sanctioned
      re-parenting path;
  (c) subtree CTE queries carry `WHERE s.lvl < 10` as a fuse against
      pre-existing corruption.
"""
import pytest
import pymysql


pytestmark = pytest.mark.testdb


def _insert_cat(cur, name, parent_id=None, full_label=None, depth=0):
    cur.execute(
        "INSERT INTO sample_category (parent_id, name, full_label, depth) "
        "VALUES (%s, %s, %s, %s)",
        (parent_id, name, full_label or name, depth),
    )
    return cur.lastrowid


class TestTriggerBlocksSelfParenting:
    def test_insert_with_self_as_parent_rejected(self, test_db_conn):
        """Trigger `trg_cat_ins_no_self` catches this at INSERT time.
        Note: INSERT auto-assigns id, so this test uses UPDATE-after-insert
        as the only way to construct 'parent_id = self.category_id'."""
        with test_db_conn.cursor() as c:
            cid = _insert_cat(c, "loop")
            with pytest.raises(pymysql.err.OperationalError,
                               match="cannot be its own parent"):
                c.execute("UPDATE sample_category SET parent_id = %s "
                          "WHERE category_id = %s", (cid, cid))

    def test_trigger_fires_on_update_only_when_self(self, test_db_conn):
        """Sanity: legitimate re-parenting (to a DIFFERENT node) is NOT
        blocked by the trigger — that's the procedure's job."""
        with test_db_conn.cursor() as c:
            a = _insert_cat(c, "A")
            b = _insert_cat(c, "B")
            c.execute("UPDATE sample_category SET parent_id = %s "
                      "WHERE category_id = %s", (a, b))


class TestMoveCategoryProcedure:
    def test_valid_move_succeeds(self, test_db_conn):
        with test_db_conn.cursor() as c:
            root = _insert_cat(c, "酒類")
            beer = _insert_cat(c, "Beer", parent_id=root)
            new_bucket = _insert_cat(c, "Alcohol")
            c.callproc("move_category", (beer, new_bucket))
            c.execute("SELECT parent_id FROM sample_category "
                      "WHERE category_id = %s", (beer,))
            assert c.fetchone()[0] == new_bucket

    def test_move_to_self_rejected(self, test_db_conn):
        with test_db_conn.cursor() as c:
            node = _insert_cat(c, "solo")
            with pytest.raises(pymysql.err.OperationalError,
                               match="cannot be its own parent"):
                c.callproc("move_category", (node, node))

    def test_move_into_direct_child_rejected(self, test_db_conn):
        """DESIGN §3c: procedure runs subtree CTE and refuses if the new
        parent is any descendant (including a direct child)."""
        with test_db_conn.cursor() as c:
            parent = _insert_cat(c, "P")
            child = _insert_cat(c, "C", parent_id=parent)
            with pytest.raises(pymysql.err.OperationalError, match="cycle"):
                c.callproc("move_category", (parent, child))

    def test_move_into_deep_descendant_rejected(self, test_db_conn):
        """A -> B -> C -> D; moving A under D would create a cycle."""
        with test_db_conn.cursor() as c:
            a = _insert_cat(c, "A")
            b = _insert_cat(c, "B", parent_id=a)
            c_ = _insert_cat(c, "C", parent_id=b)
            d = _insert_cat(c, "D", parent_id=c_)
            with pytest.raises(pymysql.err.OperationalError, match="cycle"):
                c.callproc("move_category", (a, d))

    def test_move_to_null_parent_makes_root(self, test_db_conn):
        with test_db_conn.cursor() as c:
            parent = _insert_cat(c, "P")
            child = _insert_cat(c, "C", parent_id=parent)
            c.callproc("move_category", (child, None))
            c.execute("SELECT parent_id FROM sample_category "
                      "WHERE category_id = %s", (child,))
            assert c.fetchone()[0] is None


class TestSubtreeCteDepthCap:
    """DESIGN §3c layer (c): even against corrupted data (a cycle inserted
    by raw UPDATE that bypassed the procedure), the recursive CTE MUST
    terminate. The `WHERE s.lvl < 10` cap enforces this."""

    def test_nightly_integrity_query_detects_cycle(self, test_db_conn):
        """Raw UPDATE to inject a cycle (only possible outside the procedure
        — the trigger blocks self-parenting but MySQL 1442 prevents multi-
        hop detection there). The DESIGN §3c nightly SQL must catch it."""
        with test_db_conn.cursor() as c:
            a = _insert_cat(c, "A")
            b = _insert_cat(c, "B", parent_id=a)
            c_ = _insert_cat(c, "C", parent_id=b)
            # inject the cycle A -> B -> C -> A via raw UPDATE
            c.execute("UPDATE sample_category SET parent_id = %s "
                      "WHERE category_id = %s", (c_, a))

            # DESIGN §3c nightly integrity check
            c.execute("""
                WITH RECURSIVE walk AS (
                    SELECT category_id AS start_id, parent_id AS cur_id, 1 AS steps
                    FROM sample_category WHERE parent_id IS NOT NULL
                    UNION ALL
                    SELECT w.start_id, c.parent_id, w.steps + 1
                    FROM walk w JOIN sample_category c ON c.category_id = w.cur_id
                    WHERE c.parent_id IS NOT NULL AND w.steps < 32
                )
                SELECT DISTINCT start_id FROM walk WHERE cur_id = start_id
                ORDER BY start_id
            """)
            in_cycle = {r[0] for r in c.fetchall()}
            assert in_cycle == {a, b, c_}, (
                "all three cycle members should be detected as in-cycle")

    def test_subtree_cte_terminates_on_cycle(self, test_db_conn):
        """A subtree query started INSIDE a cycle must not hang — the cap
        keeps recursion depth bounded (~10 iterations, empty result rather
        than infinite loop)."""
        with test_db_conn.cursor() as c:
            c.execute("SET SESSION cte_max_recursion_depth = 32")
            a = _insert_cat(c, "A")
            b = _insert_cat(c, "B", parent_id=a)
            c.execute("UPDATE sample_category SET parent_id = %s "
                      "WHERE category_id = %s", (b, a))  # A <-> B
            c.execute("""
                WITH RECURSIVE subtree AS (
                    SELECT category_id, 0 AS lvl
                    FROM sample_category WHERE category_id = %s
                    UNION ALL
                    SELECT c.category_id, s.lvl + 1
                    FROM sample_category c
                    JOIN subtree s ON c.parent_id = s.category_id
                    WHERE s.lvl < 10
                )
                SELECT MAX(lvl) FROM subtree
            """, (a,))
            max_lvl = c.fetchone()[0]
            assert max_lvl is not None
            assert max_lvl <= 10, "depth cap did not fire; query would hang"
