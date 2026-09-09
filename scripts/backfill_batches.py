"""Backfill batches for measurements ingested before v1.1. Version 1.1.

For each distinct folder_name in `measurement`:
  1. Re-classify sample_type using the updated sample_type_from_name()
     (v1.1 catches Testmix / Ketone Mix as standards).
  2. Create a batch row with label = folder_name.
  3. Auto-detect batch STD (first sample_type='standard' in folder) and
     BLANK (first sample_type='blank' in folder); link both if present.
  4. Set measurement.batch_id for every row in that folder.

Idempotent: rerunning does nothing if the batches are already there and
the classifications are already correct.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).parent))
from mea_parser import sample_type_from_name  # noqa: E402


def _reclassify(conn) -> int:
    """UPDATE any rows whose sample_type differs from what the current
    classifier would produce. Returns count changed."""
    changed = 0
    with conn.cursor() as c:
        c.execute("SELECT mea_id, sample_name, sample_type FROM measurement")
        rows = c.fetchall()
    for mea_id, name, cur_type in rows:
        new_type = sample_type_from_name(name)
        if new_type != cur_type:
            with conn.cursor() as c:
                c.execute("UPDATE measurement SET sample_type = %s WHERE mea_id = %s",
                          (new_type, mea_id))
            changed += 1
            print(f"  reclassified mea_id={mea_id} {name!r}: {cur_type} -> {new_type}")
    return changed


def _find_batch(conn, label: str):
    with conn.cursor() as c:
        c.execute("SELECT batch_id, std_mea_id, blank_mea_id FROM batch WHERE label = %s",
                  (label,))
        return c.fetchone()


def _find_std_and_blank(conn, folder_name: str):
    """Pick one standard + one blank per folder. If multiple exist, choose
    the earliest by measured_at (typically the batch's first calibration)."""
    with conn.cursor() as c:
        c.execute("""SELECT mea_id FROM measurement
                     WHERE folder_name = %s AND sample_type = 'standard'
                     ORDER BY measured_at ASC LIMIT 1""", (folder_name,))
        std = c.fetchone()
        c.execute("""SELECT mea_id FROM measurement
                     WHERE folder_name = %s AND sample_type = 'blank'
                     ORDER BY measured_at ASC LIMIT 1""", (folder_name,))
        blank = c.fetchone()
    return (std[0] if std else None,
            blank[0] if blank else None)


def _upsert_batch(conn, label: str, std_id, blank_id) -> int:
    existing = _find_batch(conn, label)
    with conn.cursor() as c:
        if existing:
            batch_id, cur_std, cur_blank = existing
            if cur_std != std_id or cur_blank != blank_id:
                c.execute("""UPDATE batch SET std_mea_id = %s, blank_mea_id = %s
                             WHERE batch_id = %s""", (std_id, blank_id, batch_id))
                print(f"  updated batch {batch_id!r} ({label!r}): std={std_id} blank={blank_id}")
            return batch_id
        c.execute("""INSERT INTO batch (label, std_mea_id, blank_mea_id)
                     VALUES (%s, %s, %s)""", (label, std_id, blank_id))
        print(f"  created batch {c.lastrowid} ({label!r}): std={std_id} blank={blank_id}")
        return c.lastrowid


def _link_measurements(conn, folder_name: str, batch_id: int) -> int:
    with conn.cursor() as c:
        c.execute("""UPDATE measurement SET batch_id = %s
                     WHERE folder_name = %s AND (batch_id IS NULL OR batch_id != %s)""",
                  (batch_id, folder_name, batch_id))
        return c.rowcount


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--user", default="shengic")
    ap.add_argument("--password", default="sirirat")
    ap.add_argument("--database", default="gc-ims_database")
    args = ap.parse_args()

    conn = pymysql.connect(host=args.host, port=3306, user=args.user,
                          password=args.password, database=args.database,
                          autocommit=True, charset="utf8mb4")

    print("[1/3] Reclassifying sample_type per v1.1 rules ...")
    n_reclass = _reclassify(conn)
    print(f"  {n_reclass} rows changed\n")

    print("[2/3] Building batches per folder_name ...")
    with conn.cursor() as c:
        c.execute("""SELECT DISTINCT folder_name FROM measurement
                     WHERE folder_name IS NOT NULL ORDER BY folder_name""")
        folders = [r[0] for r in c.fetchall()]
    batch_map = {}
    for folder in folders:
        std_id, blank_id = _find_std_and_blank(conn, folder)
        batch_id = _upsert_batch(conn, folder, std_id, blank_id)
        batch_map[folder] = batch_id
    print()

    print("[3/3] Linking measurements to batches ...")
    total_linked = 0
    for folder, batch_id in batch_map.items():
        n = _link_measurements(conn, folder, batch_id)
        if n:
            print(f"  folder {folder!r} -> batch_id={batch_id}: {n} measurements linked")
        total_linked += n
    print(f"  {total_linked} total measurements linked\n")

    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM batch")
        n_batches = c.fetchone()[0]
        c.execute("""SELECT COUNT(*) FROM batch
                     WHERE std_mea_id IS NOT NULL AND blank_mea_id IS NOT NULL""")
        n_full = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM measurement WHERE batch_id IS NOT NULL")
        n_linked = c.fetchone()[0]
    print(f"SUMMARY: {n_batches} batches ({n_full} with both STD+BLANK), "
          f"{n_linked} measurements linked")
    conn.close()


if __name__ == "__main__":
    main()
