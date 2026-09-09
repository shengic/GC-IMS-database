<!-- Version 1.1 -->
# GC-IMS Measurement Database

MySQL database + Tkinter desktop apps for storing, searching, and
quick-viewing G.A.S. FlavourSpec GC-IMS `.mea` measurement files.

## Repository layout
- `CLAUDE.md` — project instructions for Claude Code (hard constraints)
- `docs/DESIGN.md` — authoritative design-decision record (English)
- `docs/DESIGN_zh-TW.md` — Chinese reading snapshot (authoritative version is DESIGN.md)
- `docs/schema-diagram.png` — ERD overview
- `schema/gcims_schema.sql` — full MySQL 8 DDL (15 tables + procedure + triggers + 4 CHECK constraints)
- `sample/README.md` — sample .mea inventory (8 file types, pinned by SHA-256)
- `scripts/`
  - `mea_parser.py` — pure-function parser (split_mea, promote, find_rip, telemetry)
  - `mea_preview.py` — max-pool + npz + pixel-dump heatmap
  - `ingest_mea.py` — batch ingest CLI (`.mea` → measurement + preview_npz + mea_file)
  - `render_previews.py` — deferred heatmap/thumb PNG generator (matplotlib figure-style; RIP-normalized X)
  - `apply_schema.py` — safe DROP/RECREATE utility (default / --drop-first / --drop-database)
- `tests/` — pytest suite (190 tests: parser + preview unit; DB triggers/procedure/CHECKs; live-DB integrity scan)

## Status
**Version 1.1** — batch model added on top of v1.0's ingest pipeline.
- Design phase: complete. 21+ recorded decisions in `docs/DESIGN.md`.
- Schema: 15 tables + `move_category` procedure + 2 anti-cycle triggers + 4 CHECK constraints.
- Cross-validation: 8 real `.mea` file TYPES across 5 firmware generations (2.16/2.29/2.52/4.73/4.82) and 2 product lines (FlavourSpec dual-EPC + GC-IMS pump-controlled).
- Ingest pipeline: implemented. 49 files loaded; SHA-256 roundtrip verified on all.
- **Batch model (v1.1)**: `batch` table + `measurement.batch_id` FK. Auto-populated by folder_name; STD.mea / BLANK.mea recognised by sample_type and linked to their batch. 6 batches, 2 with complete STD+BLANK coverage.
- Preview generation: two-stage per §5 step 8 — fast ingest stores `preview_npz` only, `render_previews.py` fills PNGs (readGAS-style figure by default).
- QC: 205 tests, 30 of them integrity scans against the live DB (SHA + npz + PNG + registry + audit-trail + batch coverage).
- Next: admin app (batch edit + category management), search app, and RI-axis rendering that consumes the STD calibration.

## Quick start for Claude Code
Open this folder; CLAUDE.md loads automatically. Then:
read docs/DESIGN.md and schema/gcims_schema.sql before writing any code.

## Common commands
```
# ingest new .mea files
python scripts/ingest_mea.py "mea data"

# fill/refresh heatmap PNGs (deferred render, reads only preview_npz)
python scripts/render_previews.py                # missing only
python scripts/render_previews.py --force        # rebuild every row

# rebuild batches for already-ingested rows (idempotent)
python scripts/backfill_batches.py

# reset a database (nuclear — needs shengic DBA account)
python scripts/apply_schema.py --database gc-ims_database_test --drop-database --yes

# QA
pytest                        # everything
pytest -m "not slow"          # skip the SHA-256 scan (~10 s)
pytest -m "not db and not testdb"   # pure functions only
```
