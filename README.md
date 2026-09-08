# GC-IMS Measurement Database

MySQL database + Tkinter desktop apps for storing, searching, and
quick-viewing G.A.S. FlavourSpec GC-IMS `.mea` measurement files.

## Repository layout
- `CLAUDE.md` — project instructions for Claude Code (hard constraints)
- `docs/DESIGN.md` — authoritative design-decision record (English)
- `docs/DESIGN_zh-TW.md` — Chinese reading snapshot (authoritative version is DESIGN.md)
- `docs/schema-diagram.png` — ERD overview
- `schema/gcims_schema.sql` — full MySQL 8 DDL (12 tables, triggers, procedure, grants)
- `sample/README.md` — sample .mea inventory (files stored on NAS, identified by SHA-256)

## Status
Design phase complete; schema cross-validated against four real .mea files
spanning three firmware generations (2.16 / 2.52 / 4.82, 2014–2025).
Implementation (ingest pipeline + admin app + search app) is next.

## Quick start for Claude Code
Open this folder; CLAUDE.md loads automatically. Then:
read docs/DESIGN.md and schema/gcims_schema.sql before writing any code.
