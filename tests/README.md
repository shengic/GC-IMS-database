<!-- Version 1.1 -->
# Tests

QC for the ingest pipeline, schema, and rendering. Two tiers:

- **Unit** (fast, no I/O beyond reading sample .mea files) — pure functions
  in `scripts/mea_parser.py` and `scripts/mea_preview.py`.
- **DB** (needs live MySQL) — schema invariants: anti-cycle triggers,
  `move_category` procedure, dedup, header registry upsert.
- **Integration** (slowest, DB + files) — end-to-end ingest and render
  from `.mea` bytes to populated rows.

Run:
```
pytest                        # all tests
pytest -m "not db"            # skip live-DB tests
pytest tests/test_parser_split.py -v
```

## What each test guards against

### Unit — `scripts/mea_parser.py`
| test | invariant | source |
|---|---|---|
| `test_parser_split.py`     | `split_mea` byte-exact reshape on all 8 file TYPES; delta=0; correct key counts | DESIGN §2b, sample/README.md |
| `test_parser_rip.py`       | `find_rip` matches VOCal (RT=0 row + start=200); rejects the drift-18 pathological case; polarity + weak-flag semantics | DESIGN §5b |
| `test_parser_promote.py`   | HEADER_KEY_ALIASES covers all 6 firmware generations; absent keys → None (never raise); numeric extraction from '150 [kHz]'; multi-format dates; 'off'/'xxx' → None | DESIGN §2f |
| `test_parser_telemetry.py` | All 7 series (flow_ims/gc, press_ims/gc/ambient, pump1_flow/pump1_pressure) parsed; non-numeric tokens dropped | DESIGN §2f, §3 run_telemetry |
| `test_parser_helpers.py`   | `sample_type_from_name` classification; `parse_program` fallback to '(unparsed)'; `program_hash` stability + None-tolerance | DESIGN §3, §19 |

### Unit — `scripts/mea_preview.py`
| test | invariant | source |
|---|---|---|
| `test_preview_max_pool.py` | Output shape = ceil-div by pool factors; max-pool preserves narrow peaks (a synthetic 1-cell spike survives) | DESIGN §17 |
| `test_preview_npz.py`      | Roundtrip: `np.load(BytesIO(pack_npz(...)))` returns arrays byte-identical to inputs | — |
| `test_preview_render.py`   | `render_heatmap_png` returns valid PNG bytes; `make_thumb_png` respects target_width and passes through if input smaller | DESIGN §17 |

### DB — schema invariants
| test | invariant | source |
|---|---|---|
| `test_db_category.py`     | Trigger blocks self-parenting; raw `UPDATE parent_id` can create a cycle (detected only by the nightly integrity SQL); `move_category` procedure rejects a move into a descendant; every subtree CTE with `WHERE s.lvl < 10` terminates | DESIGN §3c |
| `test_db_dedup.py`        | Duplicate `file_hash` INSERT → IntegrityError on `uk_hash` | DESIGN §14 |
| `test_db_registry.py`     | `INSERT ... ON DUPLICATE KEY UPDATE occurrences = occurrences + 1` semantics | DESIGN §5 step 4 |
| `test_db_soft_delete.py`  | `retired=1` filters out of default list; `retired=0` toggle brings back | DESIGN §15 |
| `test_db_batch.py`        | v1.1: batch.label UNIQUE; std_mea_id/blank_mea_id nullable FKs to measurement; measurement.batch_id nullable FK; ON DELETE SET NULL both directions | DESIGN §21 |
| `test_db_grants.py`       | `gcims_viewer` can `SELECT`, cannot `INSERT` on `measurement` (post-grants only) | DESIGN §8 |

### Integration — end-to-end
| test | invariant | source |
|---|---|---|
| `test_ingest_e2e.py`      | Ingest one file → measurement + preview_npz + mea_file + telemetry + registry all populated; SHA-256 roundtrip via zstd decompress; second ingest → 'duplicate'; re-ingest never touches description/note/category_id | DESIGN §5, §10, §14 |
| `test_render_e2e.py`      | `render_previews.py --mea-id N` fills heatmap_png + thumb_png + png_render_ver; `--force` overwrites | DESIGN §5 step 8, §5d |
| `test_hash_verify.py`     | Decompress every mea_file.content, sha256 → matches measurement.file_hash. The retrieval invariant per DESIGN §14. |

## Fixture strategy

`conftest.py` provides:
- `SAMPLE_FILES` — dict `{firmware -> Path}` selected from `mea data/`. Skips tests
  that need a specific file when absent.
- `raw_bytes(sample_file)` — reads the file once per session.
- `parsed_file(sample_file)` — session-cached `(header, matrix, meta)` tuple.
- `db_conn` — pymysql to `gc-ims_database` (read-only tests) — session scope.
- `test_db_conn` — pymysql to `gc-ims_database_test` (destructive tests). Auto-
  skips if the test DB is not present; setup guide in this README.

## Setting up the test DB (for DB / integration tests)

Use the `apply_schema.py` utility — it handles DROP + CREATE + apply,
including DELIMITER blocks:

```
# fresh test DB (nuclear reset)
python scripts/apply_schema.py --database gc-ims_database_test --drop-database --yes

# wipe objects but keep DB + GRANTs
python scripts/apply_schema.py --database gc-ims_database_test --drop-first --yes

# apply schema onto an empty DB (default)
python scripts/apply_schema.py --database gc-ims_database_test
```

Tests use a function-scoped `test_db_conn` fixture (autocommit=False +
rollback in teardown), so parallel-safe cleanup is automatic per test.
DDL (CREATE/DROP) auto-commits in MySQL — the DB test suite doesn't use
any DDL inside tests to preserve this isolation.

## Phase 2 additions (not yet written)

- Property-based tests (Hypothesis) for `promote()` value extraction
- Regression tests: pin the exact PNG bytes for one canonical file (detects
  accidental recipe drift). Use image-diff so LUT-anchor tweaks don't break.
- Rendering perf test: assert render_previews.py hits ≤1 s / 90 MB file on
  reference hardware (guards against a slow matplotlib config change).
- Row-count regression: `pytest --benchmark` for ingest throughput.
