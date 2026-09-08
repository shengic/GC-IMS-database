"""Apply schema/gcims_schema.sql to a target MySQL database. Version 1.0.

Handles:
- DELIMITER blocks (a `mysql` CLI directive that pymysql cannot parse).
- Skips `CREATE DATABASE` / `USE` lines in the schema file — we connect
  directly to the target database instead.
- Ignores semicolons inside `-- comments` when splitting statements.

Modes (in ascending order of destructiveness):
    (default)          Apply schema to an empty target. Fails if any table
                       already exists (schema uses CREATE TABLE, not
                       CREATE TABLE IF NOT EXISTS).
    --drop-first       Drop every table + procedure inside the target DB,
                       then apply. Keeps the DB itself (GRANTs preserved).
    --drop-database    DROP DATABASE + CREATE DATABASE + apply. Nuclear.
                       Also wipes any account-scoped GRANTs on the DB.

Confirmation is required unless --yes is passed.

Examples:
    # Fresh test DB for the QC suite
    python scripts/apply_schema.py --database gc-ims_database_test \\
        --drop-database --yes

    # Wipe production DB clean and reapply (careful!)
    python scripts/apply_schema.py --database gc-ims_database \\
        --drop-first --yes
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

import pymysql


def split_sql(text: str) -> list[str]:
    """Split SQL text by delimiter, respecting DELIMITER directives and
    ignoring semicolons inside `-- comments`."""
    delim = ";"
    statements: list[str] = []
    buf: list[str] = []
    for raw in text.splitlines(keepends=True):
        s = raw.strip()
        if s.upper().startswith("DELIMITER "):
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            delim = s.split(None, 1)[1].strip()
            continue
        buf.append(raw)
        code = re.sub(r"--.*$", "", s).rstrip()
        if code.endswith(delim):
            stmt = "".join(buf).rstrip()
            if stmt.endswith(delim):
                stmt = stmt[: -len(delim)].rstrip()
            if stmt:
                statements.append(stmt)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _is_pure_comment(stmt: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--")
               for line in stmt.splitlines())


def _is_db_scope(stmt: str) -> bool:
    head = re.sub(r"--[^\n]*\n", "", stmt).strip().upper()
    return head.startswith("CREATE DATABASE") or head.startswith("USE ")


def drop_all_objects(conn):
    """Drop every table + procedure inside the current database.
    Triggers drop with their tables. FK checks disabled to bypass ordering."""
    with conn.cursor() as c:
        c.execute("SET FOREIGN_KEY_CHECKS = 0")
        c.execute("SHOW TABLES")
        for (t,) in c.fetchall():
            c.execute(f"DROP TABLE IF EXISTS `{t}`")
        c.execute("SET FOREIGN_KEY_CHECKS = 1")
        c.execute("""SELECT ROUTINE_NAME FROM information_schema.ROUTINES
                     WHERE ROUTINE_SCHEMA = DATABASE()""")
        for (r,) in c.fetchall():
            c.execute(f"DROP PROCEDURE IF EXISTS `{r}`")


def apply_schema(conn, schema_path: str) -> tuple[int, int]:
    sql = Path(schema_path).read_text(encoding="utf-8")
    statements = [s for s in split_sql(sql)
                  if not _is_pure_comment(s) and not _is_db_scope(s)]
    ok = fail = 0
    with conn.cursor() as c:
        for stmt in statements:
            try:
                c.execute(stmt)
                ok += 1
            except Exception as e:
                fail += 1
                head = re.sub(r"\s+", " ", re.sub(r"--[^\n]*\n", "", stmt))[:80]
                print(f"  FAIL: {head}")
                print(f"        -> {type(e).__name__}: {e}")
    return ok, fail


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        return False


def _summarize(conn):
    with conn.cursor() as c:
        c.execute("SHOW TABLES")
        n_tables = len(c.fetchall())
        c.execute("""SELECT COUNT(*) FROM information_schema.ROUTINES
                     WHERE ROUTINE_SCHEMA = DATABASE()""")
        n_routines = c.fetchone()[0]
        c.execute("""SELECT COUNT(*) FROM information_schema.TRIGGERS
                     WHERE TRIGGER_SCHEMA = DATABASE()""")
        n_triggers = c.fetchone()[0]
    return n_tables, n_routines, n_triggers


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__[__doc__.index("Modes"):],
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="shengic")
    ap.add_argument("--password", default="sirirat")
    ap.add_argument("--database", required=True)
    ap.add_argument("--schema", default="schema/gcims_schema.sql")
    ap.add_argument("--drop-first", action="store_true",
                    help="drop all tables + procedures in target DB first")
    ap.add_argument("--drop-database", action="store_true",
                    help="DROP DATABASE + CREATE DATABASE + apply (nuclear)")
    ap.add_argument("--yes", action="store_true",
                    help="skip confirmation prompt")
    args = ap.parse_args()

    if args.drop_first and args.drop_database:
        sys.exit("--drop-first and --drop-database are mutually exclusive")

    destructive = args.drop_first or args.drop_database
    if destructive and not args.yes:
        action = "DROP DATABASE" if args.drop_database else "DROP ALL OBJECTS"
        prompt = (f"About to {action} on `{args.database}` "
                  f"@ {args.host}:{args.port}. Continue? [y/N] ")
        if not _confirm(prompt):
            sys.exit("aborted")

    common = dict(host=args.host, port=args.port, user=args.user,
                  password=args.password, charset="utf8mb4", autocommit=True,
                  connect_timeout=5)

    if args.drop_database:
        srv = pymysql.connect(**common)
        with srv.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS `{args.database}`")
            c.execute(f"CREATE DATABASE `{args.database}` "
                      f"DEFAULT CHARACTER SET utf8mb4 "
                      f"COLLATE utf8mb4_0900_ai_ci")
        srv.close()
        print(f"database `{args.database}` dropped and recreated (empty)")
    else:
        srv = pymysql.connect(**common)
        with srv.cursor() as c:
            c.execute(f"CREATE DATABASE IF NOT EXISTS `{args.database}` "
                      f"DEFAULT CHARACTER SET utf8mb4 "
                      f"COLLATE utf8mb4_0900_ai_ci")
        srv.close()

    conn = pymysql.connect(database=args.database, **common)
    if args.drop_first and not args.drop_database:
        drop_all_objects(conn)
        print(f"dropped all objects in `{args.database}`")

    print(f"applying {args.schema} to `{args.database}`...")
    ok, fail = apply_schema(conn, args.schema)
    n_tables, n_routines, n_triggers = _summarize(conn)
    conn.close()

    print(f"\n  {ok} statements ok, {fail} failed")
    print(f"  final state: {n_tables} tables, {n_routines} procedures, {n_triggers} triggers")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
