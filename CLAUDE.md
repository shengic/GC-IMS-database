<!-- Version 1.1 -->
# GC-IMS Measurement Database

MySQL database + Tkinter desktop app for storing, searching, and quick-viewing
G.A.S. FlavourSpec GC-IMS `.mea` files.

## Read first
- `schema/gcims_schema.sql` — authoritative DDL (15 tables + procedure + 2 triggers + 4 CHECK constraints). Do not restructure without reading `docs/DESIGN.md`.
- `docs/DESIGN.md` — why the schema is designed this way. Every non-obvious decision is recorded there.
- `scripts/mea_parser.py` — reference implementation of `split_mea` / `find_rip` / `promote` / `parse_telemetry`. Match this when writing any other .mea reader.
- `tests/README.md` — QC plan, marker conventions (`db`, `testdb`, `slow`), and how to spin up `gc-ims_database_test`.

## .mea file format (verified on real file)
- Text header: latin-1, `key = value` lines (key set varies by firmware), followed by the binary matrix. SPLIT METHOD: primary = arithmetic back-calculation (boundary = file_size - rows*cols*2, ints regex'd from leading text); secondary cross-check = first non-printable byte; the two must agree within a few bytes or the file is flagged parse_error (DESIGN.md §2b).
- Binary body: int16 little-endian matrix, shape = (`Chunks count`, `Chunk sample count`) — e.g. 8571x4500 (fw 2.52) or 14289x3150 (fw 4.82). Byte count matches exactly: total_size - header_bytes = rows * cols * 2.
- Signal polarity is PER-FILE (verified across firmware generations 2.16/2.29/2.52/4.73/4.82 — no firmware-level rule exists). RIP detection uses VOCal-compatible method (matches GC-IMS-PEAK/rip.py for cross-tool alignment): argmax over RT=0 row after skipping first 200 drift samples. Signed value gives polarity; weak-RIP (winner < 3x row0 tail median) -> ingest_log 'ok_with_warnings'. See DESIGN §5b.
- Header key SET and NAMES vary by firmware (51 keys fw2.16, 56 fw2.29, 60 fw2.52, 70 fw4.73 GC-IMS, 68 fw4.82). ANY promoted key may be absent -> header.get() semantics, NULL, never an error. Real key names differ from earliest design assumptions: `Firmware date` (lowercase d), `Chunk sample rate`, `Chunk trigger repetition` — see the consolidated HEADER_KEY_ALIASES table in DESIGN §2f. Telemetry series count varies (2 for fw≤2.29, 5 for fw 2.5x/4.82, 7 for fw 4.73 GC-IMS with pump-1). Two product lines exist: `FlavourSpec®`/`FlavourSpecR` (dual-EPC) and `GC-IMS` (pump-controlled, serial 5F1-xxxxx). Value formats drift ("Apr 25 2014" vs ISO dates; quoted "off"; 'xxx' placeholders) -> regex-extract numbers, multi-format dates, else NULL (raw always in header_json). Ingest uses HEADER_KEY_ALIASES; unknown keys -> header_json + header_key_registry, never a failure.
- Validated against 8 file TYPES (5 firmware generations, 2014-2026, 5 instrument serials, 2 product lines); e.g. 77.1 MB (zstd 4.1x), 90.0 MB (zstd 3.7x), 151 MB (fw2.52 60-min ramp). New file types follow the same cross-validation procedure (DESIGN §2b-§2g). `.s.mea` filename extension is valid (fw 2.29 era) — glob both `*.mea` and `*.s.mea`.

## Non-negotiable architecture rules
1. Everything lives in MySQL — no external file dependency at runtime.
   Original .mea -> `mea_file.content` (LONGBLOB, zstd). Filesystem may be unavailable; DB is single source of truth.
2. Header stored 4 ways, each serving a purpose (redundancy is intentional):
   - ~22 promoted typed columns (searchable/indexed) in `measurement` / `instrument` / `gc_method`
   - ALL keys verbatim in `measurement.header_json` (MySQL JSON column — completeness layer)
   - 5 space-delimited array keys (Flow Epc 1/2, Pressure Ambient/Epc1/Epc2) exploded into `run_telemetry` long table
   - Original bytes in `mea_file` (ultimate provenance)
3. Ingest must NEVER fail on unknown header keys: store all keys into header_json,
   promote only whitelisted keys, log new keys into `header_key_registry`.
4. Heatmap generation is TWO-STAGE (DESIGN §5 step 8 / §5d):
   - INGEST does max-pool ONCE (factors 12x6) -> np.savez_compressed(matrix, rt_axis_s, dt_axis_ms) -> `mea_preview.preview_npz` (MEDIUMBLOB, ~370-870 KB).
     ingest leaves heatmap_png / thumb_png / png_render_ver NULL.
   - `scripts/render_previews.py` fills PNGs later (readGAS-style matplotlib figure by default; --style raw for pixel dump). Reads only preview_npz + rip_drift_index — never re-decompresses mea_file. Selection modes: default (WHERE heatmap_png IS NULL), --render-ver-below N (recipe upgrade), --mea-id N, --force.
   Max-pool (not slicing) so narrow peaks survive downsampling.
5. Search/list queries must NEVER select blob columns (`content`, `preview_npz`).
   Blobs fetched only by single-row primary-key lookup.
6. Peaks stored with RIP-normalized drift time `dt_rip_rel = dt / dt_RIP` — this is
   the cross-run comparable coordinate. RIP located at ingest (drift index ~1216 in sample).
7. mea_file rows are INSERT/DELETE only, never UPDATE (binlog size).
8. Verify SHA-256 on retrieval: decompress -> hash -> compare `file_hash`.
9. Category tree (`sample_category`, adjacency list): NEVER write raw
   `UPDATE sample_category SET parent_id=...` — always call the
   `move_category()` stored procedure (full anti-cycle subtree check).
   Triggers only block self-parenting (MySQL can't do more in triggers,
   error 1442). Every subtree CTE must carry `WHERE s.lvl < 10` depth cap.
   After a move, rebuild full_label/depth for the moved subtree.
   Full rationale: docs/DESIGN.md §3c.
10. measurement now also has: full_path, folder_name, description, note,
    category_id (nullable FK -> sample_category, ON DELETE SET NULL).
11. A mea_file row may legitimately be ABSENT (tiered demo subsets carry
    all metadata/previews but only selected originals). Full-resolution
    zoom must degrade gracefully ("preview-only in this subset"), never
    crash. Deployment/remote-access/demo strategy: docs/DESIGN.md §7
    (3306 never public; service+datadir same machine; NAS = backup target).
12. TWO apps, privilege-separated by DB account (DESIGN.md §8):
    admin app -> gcims_admin (CRUD + EXECUTE move_category, no DDL);
    search app -> gcims_viewer (per-table SELECT only). View-only is
    enforced by GRANTs, not UI. Admin edits write to audit_log
    (actor/action/old/new).
13. SQL injection iron rule: ALL values via %s placeholders — never
    f-string/format/concat SQL with variables. Dynamic identifiers
    (ORDER BY columns) only via in-code whitelist dicts. IN clauses via
    generated placeholder lists. Escape % and _ in LIKE inputs.
    Treat .mea header values as untrusted data.
14. Search pagination (DESIGN.md §9): COUNT first; total > 20,000 ->
    ask user to narrow filters, don't fetch. Else fetch all light rows
    once (no blobs/header_json/full note; LEFT(description,80)),
    ORDER BY measured_at DESC, mea_id DESC; client-side paging in Tk,
    PAGE_SIZE=50. Index idx_time_id (measured_at, mea_id) supports a
    future keyset-pagination switch.
15. Field ownership (DESIGN.md §10): description / note / category_id are
    HUMAN-entered via admin UI only — ingest leaves them NULL and
    re-ingest must NEVER overwrite them. Machine-derived columns may be
    regenerated freely. Admin app provides batch edit (multi-select ->
    apply description/note/category once, each row audit-logged).
16. Soft delete (DESIGN.md §15): NEVER physically delete measurements —
    set retired=1 (+at/by/reason), audit-logged. gcims_admin has NO
    DELETE grant on measurement/dependents (only on sample_category).
    Every search/list query filters retired=0 by default; admin UI has
    a "show retired" toggle, viewer UI does not.
17. Hybrid identity (DESIGN.md §16): per-person MySQL accounts are the
    authentication + hard permission layer; gcims.app_user (mysql_user,
    display_name, role, active) is the identity layer. On connect:
    resolve CURRENT_USER() -> app_user; active=0 -> refuse; role gates
    UI features; GRANT always wins on conflict. Log download_original /
    full_resolution to access_log; never log searches/previews.
18. Scope (DESIGN.md §18): this system is the centralized .mea archive
    ONLY. Never implement peak detection or write to peak/compound —
    they are dormant reservations. Analysis tools are external readers.

## Tk app rules
- Tkinter is NOT thread-safe: DB/numpy work in worker threads, widget updates only
  in main thread via queue + root.after().
- Keep a Python reference to every PhotoImage or the image disappears (GC).
- Preview render: np.load(BytesIO(blob)) -> log/percentile normalize -> 256x3 LUT -> PIL -> ImageTk. No matplotlib dependency.
- lru_cache decoded numpy matrices (not PhotoImages) for colormap switching.
- DB connections go through named profiles in config.ini (see DESIGN.md §6b):
  passwords via the `keyring` package only, NEVER in config files or code;
  status bar always shows active profile + read-only badge; read_only
  profiles disable all write features in the UI.

## Ingest + QA workflow
- `python scripts/ingest_mea.py "mea data"` — batch ingest; per-file try/except; duplicates ok. Now auto-creates `batch` rows keyed on folder_name and links measurements + STD/BLANK pointers.
- `python scripts/backfill_batches.py` — one-shot: re-classify sample_type and rebuild batches for existing rows (safe / idempotent).
- `python scripts/render_previews.py [--force | --render-ver-below N]` — regenerate heatmap PNGs without touching mea_file. ~0.4 s per row.
- `python scripts/apply_schema.py --database DB [--drop-first | --drop-database] [--yes]` — safe recreate. Confirms unless --yes. Handles DELIMITER blocks.
- `pytest` — 205-test suite. Markers: `db` (live DB), `testdb` (destructive tests, needs `gc-ims_database_test`), `slow` (SHA-256 scan). Integrity-scan file `tests/test_integrity_scan.py` runs nightly / post-batch.

## Batch model (v1.1, DESIGN §21)
- `batch` table: `batch_id`, `label` (UNIQUE — typically the folder_name), nullable `std_mea_id` / `blank_mea_id` → measurement.
- `measurement.batch_id` FK → batch, nullable. ON DELETE SET NULL on both sides.
- STD.mea and BLANK.mea are STORED IDENTICALLY to samples — same `measurement` / `mea_file` / `mea_preview` rows, distinguished only by `sample_type`. Batch table holds POINTERS, never a second copy of the bytes.
- Auto-detection at ingest: first `sample_type='standard'` in folder → std_mea_id; first `sample_type='blank'` → blank_mea_id. Existing pointers not overwritten (admin can re-designate).
- Absence tolerated: legacy folders lacking one/both calibrations still get a batch; features needing STD (RI axis) degrade gracefully.

## Column CHECK constraints (fail-fast at INSERT/UPDATE)
Enforced by MySQL 8, not by app code. New rows violating any of these are refused by the DB:
- `ck_meas_geometry`: `file_size_bytes = header_bytes + n_spectra * n_drift_points * 2` (byte-exact reshape identity).
- `ck_meas_retired_consistency`: `retired=0` has all retire-metadata NULL; `retired=1` needs `retired_at` AND `retired_by`.
- `ck_meas_rip_index_within_axis`: `rip_drift_index IS NULL OR < n_drift_points`.
- `ck_meas_matrix_dtype`: whitelist (`'int16le'` only). Future dtypes must widen the CHECK — deliberate friction.
CHECK violations raise `pymysql.err.OperationalError` (3819), not IntegrityError.

## MySQL settings assumed
max_allowed_packet >= 256M; innodb_file_per_table = ON.
Backups: nightly `mysqldump --single-transaction --routines --triggers`
piped through zstd to the NAS (DESIGN.md §7); --routines --triggers is
mandatory or move_category and the anti-cycle triggers are lost from
restores. XtraBackup only if the library outgrows logical dumps.

## Language
UI and comments: Traditional Chinese acceptable; code identifiers in English.