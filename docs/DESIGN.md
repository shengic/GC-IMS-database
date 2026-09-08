# GC-IMS Database — Design Rationale

Companion to `schema/gcims_schema.sql`. Records the reasoning behind each
decision so future changes are made with full context.

## 1. Problem statement
Store G.A.S. FlavourSpec `.mea` measurement files so users can search
(by sample, date, method, peak position, compound) and quick-view heatmaps,
via a Tkinter desktop app connecting directly to MySQL. Requirement:
the system must work even when the original file system is unavailable,
so MySQL holds everything.

## 2. .mea format (measured on file TYPE 1: 260210_095729_FISH_MEAT_BLANK.mea)
| Property | Value |
|---|---|
| Total size | 77,144,541 bytes |
| Header | 5,541 bytes latin-1 text, 60 `key = value` lines |
| Matrix | int16 LE, 8571 spectra x 4500 drift points (byte-exact) |
| Drift axis | 4500 pts @ 150 kHz = 30.0 ms sweep |
| GC axis | 8571 chunks x 30 ms x 6 averages ~ 25.7 min |
| RIP | drift index ~1216; signal negative-going (min -464) |
| zstd L3 | 77.1 -> 18.9 MB (4.1x) |
| Preview | max-pool (12,6) -> 714x750, npz ~367 KB |

Header key set varies with machine type / firmware / autosampler presence.
Never hard-code the 60-key list as a requirement.

## 3. Table-by-table rationale (11 tables)

### measurement (hub)
One row per .mea. Holds promoted searchable columns + matrix geometry
(header_bytes, n_spectra, n_drift_points, matrix_dtype) so the binary can be
re-sliced from mea_file without re-parsing the header. `sample_type` ENUM
classified at ingest from name patterns (blank/std/qc) because "exclude blanks"
is the most common filter. `header_json` (JSON column) holds ALL header keys
verbatim — the completeness layer that makes schema evolution cheap
(promote later with one ALTER + one UPDATE backfill, no re-ingest).

### instrument, gc_method (shared dictionaries)
Normalized out because many measurements share one instrument/method.
gc_method deduplicated by `program_hash` = sha256(program string + column + gas):
same program NAME with edited parameters becomes a distinct method row —
required for traceability ("which method version produced this batch").

### run_telemetry
The five space-delimited array keys (Flow Epc 1/2, Pressure Ambient/Epc1/Epc2)
are in-run time series at `Flow record interval` (18 s) — ~85 points/series for
a 25.7 min run, ~425 rows/file. Exploded to long format because QA queries
("runs where EPC2 pressure exceeded X", "drift-gas flow stability stddev")
are impossible against a string inside JSON. Time axis not stored:
t = seq_no * record_interval. Original strings remain in header_json.

### peak, compound (search core)
`dt_rip_rel = dt / dt_RIP` is the load-bearing column: absolute drift time
drifts with temperature/pressure; only RIP-normalized values are comparable
across runs. Composite index (rt_s, dt_rip_rel) serves window searches
("peaks within rt±d, dtrel±d"); (compound_id, mea_id) serves
"which samples contain compound X". compound.compound_id nullable on peak
(unidentified peaks are normal).

### mea_preview
Downsampled matrix as npz blob (np.savez_compressed: matrix, rt_axis_s,
dt_axis_ms). Decisions:
- npz not PNG: PNG bakes in colormap/scale/clip; npz keeps numbers so
  colormap & log/linear are user-switchable at view time (~20 ms LUT pass).
  Rendering from a 714x750 npz costs <50 ms; the expensive part of
  "on-the-fly" was always parsing the 77 MB file, not rendering.
- max-pool not slicing: GC-IMS peaks are 5-10 drift points wide;
  slicing drops peaks, max-pooling preserves them.
- pool_rt/pool_dt/pipeline_ver recorded so previews can be regenerated
  selectively when the downsampling strategy changes.

### mea_file
Whole original file, zstd-compressed, LONGBLOB. Rationale:
- DB as single source of truth (stated requirement: FS may be unavailable).
- Regulatory/data-integrity: original instrument file preserved bit-perfect
  (sha-256 verified on retrieval).
- Full-resolution zoom & reprocessing: decompress -> raw[header_bytes:]
  -> np.frombuffer(int16).reshape(n_spectra, n_drift_points). ~1-3 s,
  acceptable for explicit user actions; add server-side LRU cache if needed.
- No separate HDF5 layer: parsing .mea takes 1-2 s, so architecture
  simplicity beats a second storage format.
- Chunked variant (mea_file_chunk, 8 MB pieces) only if compressed files
  approach max_allowed_packet.

### ingest_log, header_key_registry
ingest_log: audit trail (ok/duplicate/parse_error) with pipeline_ver.
header_key_registry: first_seen tracking for unknown header keys —
answers "what keys exist across my library", "which firmware introduced
key X", "which keys await disposition". Ingest never fails on unknown keys.

## 3b. sample_category + measurement provenance columns

measurement gains: full_path, folder_name (indexed — lab folder names carry
batch semantics), description (VARCHAR, listable), note (TEXT; add ngram
FULLTEXT if Chinese search needed), category_id.

sample_category: adjacency-list tree (parent_id self-reference) with
materialized path (full_label) and depth. Multi-level type hierarchy,
e.g. 酒類 > Whisky > Single Malt > Yamazaki 12. Measurements attach at ANY
level. One category per measurement (1:1); switch to a junction table only
if multi-taxonomy becomes a real need. Adjacency list + recursive CTE chosen
over closure table: hundreds of nodes, <10 depth, low churn — closure table
is overkill here. full_label gives O(1) breadcrumbs; cost is subtree label
rebuild on rename/move (low frequency, acceptable).

## 3c. Anti-cycle protection for the category tree (RECORDED DECISION)

Risk: adjacency list allows A->B->C->A; a recursive CTE on cycled data
expands until cte_max_recursion_depth (default 1000) and errors out.
Cycles can ONLY be created by UPDATE parent_id (a freshly INSERTed node has
no descendants), so protection concentrates on the move operation.

Defense in depth — three layers, all present in schema/gcims_schema.sql §8:

(a) Triggers trg_cat_ins_no_self / trg_cat_upd_no_self: block
    self-parenting. MySQL limitation: a trigger cannot SELECT the table
    being modified (error 1442), so multi-hop cycle detection is
    IMPOSSIBLE in a trigger. Triggers are the cheap net that also catches
    manual SQL-client edits.

(b) Stored procedure move_category(node, new_parent): THE ONLY sanctioned
    write path for re-parenting. Runs a full recursive subtree check —
    rejects if new_parent is the node or any descendant — then updates,
    in one statement scope. App code must call this, never raw
    UPDATE parent_id. Requires MySQL 8.0.19+ (CTE in procedures).
    Caller must rebuild full_label/depth for the moved subtree afterwards.

(c) Query-side self-protection: every subtree CTE carries a hard depth cap
    (WHERE s.lvl < 10) and sessions set cte_max_recursion_depth = 32,
    so even corrupted data cannot hang or blow up a query.

Plus a nightly integrity SQL (walk each node up to 32 steps; any node that
reaches itself is in a cycle) — detection for the "all lines of defense
failed" case. Both queries recorded as comments in the schema file.

Rejected alternative: DB-level completeness via REVOKE UPDATE + GRANT
EXECUTE only — sound but operationally heavy for a single-user desktop
system; the procedure-as-convention approach is proportionate.

## 4. Capacity & operations
~19.3 MB/file stored (18.9 file + 0.4 preview + metadata).
1,000 files ~ 19 GB; 5,000 ~ 97 GB — comfortable for single MySQL 8.
Physical backups (XtraBackup). Revisit MinIO(S3)+MySQL-index only past
~10k files / TB scale.
max_allowed_packet >= 256M both client & server. mea_file: INSERT/DELETE
only, never UPDATE (binlog row image doubles on update).
Query discipline: blob columns only via PK single-row lookup.

## 5. Ingest pipeline (one-time cost per file, ~3-5 s)
1. Read bytes -> sha256 -> reject duplicate (uk_hash)
2. Split header at first non-printable byte; parse latin-1 key=value -> dict
3. Whitelist-promote known keys to typed columns; parse Program name;
   upsert instrument & gc_method (program_hash)
4. Full dict -> header_json; every key upserted into
   header_key_registry via INSERT ... ON DUPLICATE KEY UPDATE
   occurrences = occurrences + 1 (first appearance also records
   first_mea_id + sample_value). The registry is thus an INCREMENTALLY
   MAINTAINED union of all keys ever seen — never computed by scanning
   header_json at read time; UI reads it with one O(1) SELECT.
   Deletions do not decrement (registry semantics = "ever seen");
   optional nightly recount via JSON_CONTAINS_PATH if exact counts
   are wanted
5. Explode the 5 series keys -> run_telemetry
6. np.frombuffer int16 -> matrix; locate RIP (argmax |column mean|);
   store rip_drift_index / rip_drift_ms
7. (REMOVED from scope — see §18.) Ingest never writes peak/compound;
   those tables are dormant reservations for a possible future
   analysis layer.
8. abs(matrix) -> max-pool ONCE, then three derivatives from the same
   in-memory array: (a) savez_compressed -> preview_npz;
   (b) fixed-recipe render -> heatmap_png (native resolution);
   (c) LANCZOS downscale of (b) -> thumb_png. All written with
   png_render_ver in the SAME transaction as everything else — no
   half-derived rows possible. Polarity (step 6) precedes rendering.
   Backfill pattern (recorded): when a new derived column is added to
   an already-populated library, never re-import — a small script
   SELECTs rows WHERE new_col IS NULL and derives from existing
   preview_npz (or mea_file), touching nothing else. This is the §10
   regenerable-fields principle in operational form.
9. zstd(raw) -> mea_file; write ingest_log

## 6. Tk app architecture
Direct MySQL connection (pooled), no web API layer — desktop app is Python.
Search list: metadata columns only. Quick View: single-row preview_npz fetch
in worker thread -> queue -> main-thread PhotoImage. Colormap switch reuses
cached numpy matrix (lru_cache), no DB round-trip. Full-resolution zoom:
fetch mea_file, slice sub-window, render. Cursor readout: map pixel ->
(rt_axis_s, dt_axis_ms) from the npz arrays.
Known Tk pitfalls: keep PhotoImage references; never touch widgets from
worker threads; DB credentials in config file with a SELECT-only MySQL user.

## 6b. Connection profiles (RECORDED DECISION)

Problem: company primary, tunneled remote, and local demo copies all have
different host/port/user/password/read-only status; per-use manual entry
is error-prone (worst case: editing the wrong database unknowingly).

Design: named profiles in config.ini —
  [general] default_profile = ...
  [profile:NAME] label / host / port / database / user / read_only
  optional tunnel fields (tunnel=true, tunnel_host/user/key) for
  auto-established SSH tunnels via the sshtunnel package.
Passwords are NEVER stored in config.ini: OS keyring via the `keyring`
package (Windows Credential Manager / macOS Keychain / Secret Service),
keyed "gcims"/"{profile}:{user}"; prompt once per machine, store, reuse.
config.ini is therefore secret-free and safe to share/commit.

App behavior requirements:
- Startup dialog lists profiles by label, preselects last-used; Enter
  connects. CLI override --profile NAME skips the dialog (enables
  per-profile desktop shortcuts for demos).
- Status bar PERMANENTLY shows the active profile label (+ read-only
  badge); click to switch (close pool, open pool, refresh views).
- profile read_only=true disables all write features in the UI (notes,
  category moves, ingest) — pairs with the DB-level SELECT+EXECUTE-only
  demo account for two-layer protection.
- connection_timeout=5 so a wrong/unreachable profile fails fast instead
  of freezing the UI.

## 7. Deployment topology & remote access (RECORDED DECISION)

### Where MySQL and its data live
Rule: the MySQL SERVICE and its DATADIR must be on the SAME machine,
on local disk. Never place datadir on an SMB/NFS mount (InnoDB depends on
fsync semantics and file locking that network filesystems do not honor;
a dropped connection can corrupt tablespaces). iSCSI block mount is the
only technically safe NAS-storage exception and is not needed at this scale.

Chosen: Plan B — MySQL 8 on an always-on LAN PC (local SSD datadir);
NAS is the BACKUP TARGET only. Nightly job:
  mysqldump --single-transaction gcims | zstd > backup.sql.zst -> copy to NAS
Retention: 7 daily + weekly. Logical dump suffices at <100 GB scale;
upgrade to XtraBackup physical backups if restore-time ever matters.
This also satisfies "data must reside on NAS" policies via a daily copy.
The host PC must not sleep; MySQL service set to auto-start.

Company backup policy integration: the company backs up NAS content ONLY.
The nightly dump landing on NAS is therefore the hand-off point into
company-level protection (tape/offsite/snapshots) — the PC-resident
primary needs no separate enrollment. Chain: PC dies -> restore from
latest NAS dump (<=1 day loss); NAS dies -> company restores NAS.
Requirements this imposes:
- CONFIRM with IT that the chosen NAS folder is inside the routine backup
  scope and note its retention (shares/dirs are sometimes excluded).
- mysqldump MUST include --routines --triggers, otherwise move_category()
  and the anti-cycle triggers are silently missing from restores.
- Quarterly restore drill: zstd -d | mysql on a scratch machine, verify
  table/row counts and that procedures/triggers exist. An untested backup
  is not a backup.
- Instrument-original .mea files should ALSO be archived to NAS at ingest
  time (even though mea_file holds a copy), so both the raw-data line and
  the database line sit inside company backup coverage.

Fallback Plan A (only if policy demands the primary live on NAS and the
NAS is x86): run MySQL/MariaDB on the NAS itself (package or Docker) —
service and data still same-machine. Accept weaker CPU/RAM; size
innodb_buffer_pool_size conservatively. ARM entry NAS: not recommended.

Rejected Plan C: MySQL on PC + datadir on NAS mount — corruption risk,
see rule above.

Connectivity basics: MySQL listens on 3306 bound to the LAN IP (never
0.0.0.0-to-WAN); app account restricted by source subnet
('gcims_app'@'192.168.1.%') with SELECT/INSERT/DELETE + EXECUTE on
move_category only. Port 22 (SSH) is unrelated to MySQL connectivity.

### Remote / outside-office access
Rule: 3306 is NEVER exposed to the public internet. External access goes
through an encrypted tunnel into the LAN, then to MySQL:
- VPN (company VPN or self-hosted WireGuard; WireGuard is silent to
  scanners — near-zero attack surface, one UDP port)
- SSH tunnel (ssh -L 3306:dbhost:3306 user@bastion; key-only auth);
  Python side can automate via the sshtunnel package
- Tailscale / Cloudflare Tunnel when no inbound port can be opened at all
  (outbound-initiated; Tailscale = ad-hoc virtual LAN, easy to remove)

### Off-site demo strategy (thousands of files)
Storage math: metadata + previews for the ENTIRE library are tiny
(~0.5 GB per 1,000 files); mea_file blobs are 95% of the volume
(~19 GB per 1,000 files). Therefore:

Tiered portable demo DB (chosen):
- Copy ALL rows of measurement / peak / sample_category / instrument /
  gc_method / compound / mea_preview -> full search + category browsing +
  Quick View heatmaps work for the whole library on a laptop (few GB).
- Copy mea_file rows ONLY for the 50-100 samples the demo will deep-dive.
- Tk app degrades gracefully: full-resolution zoom on an absent mea_file
  row shows "preview-only in this demo subset" instead of failing.
Refresh via make_demo_db.py (selective mysqldump; --where on mea_file),
minutes per refresh. Optional heavier alternative: laptop as a replica
with REPLICATE_IGNORE_TABLE=gcims.mea_file, auto-catch-up on LAN.
Live fallback during a demo: tunnel (above) to fetch a single missing
original file on demand. Offline copy remains the primary demo plan —
never depend on venue network.

Portable-copy discipline: laptop copies are SNAPSHOTS and must be treated
as READ-ONLY. Any edit (notes, descriptions, category moves) made on a
copy will NOT flow back to the primary; all writes happen against the
primary (on-LAN or via tunnel). Enforce cheaply: create only a
SELECT+EXECUTE user on demo databases, or set them read_only=ON.
Access summary: LAN = direct 3306; remote = tunnel first (full function,
blob fetches bandwidth-bound); offline = tiered snapshot (all metadata +
all previews, selected originals, graceful degradation).

## 8. Two-app architecture & security model (RECORDED DECISION)

Two separate applications, privilege-separated at the DATABASE level:
- Admin app: batch + UI import of .mea, edit description/note/full_path,
  category management (via move_category). DB account gcims_admin:
  SELECT/INSERT/UPDATE/DELETE + EXECUTE move_category. No DDL, no GRANT —
  schema changes are DBA-only, so a leaked admin credential cannot destroy
  structure or mint accounts.
- Search app: search / browse / view only. DB account gcims_viewer:
  per-table SELECT only (no EXECUTE; no access to ingest_log,
  header_key_registry, audit_log). "View-only" is enforced by GRANTs,
  not by hiding buttons — UI restrictions can be bypassed, GRANTs cannot.
Profile separation (see 6b): each app's config.ini contains only its own
account's profiles; admin credentials never ship with the search app.

SQL injection policy (iron rule for all code):
- Every VALUE goes through driver placeholders (%s + params tuple).
  NO SQL assembled with f-string/+/format containing variables.
- Sole exception: identifiers (column names for ORDER BY etc.) cannot be
  parameterized — they must come from an in-code WHITELIST dict mapping
  UI choices to column names; anything not in the map is rejected.
- IN clauses: generate placeholder lists (",".join(["%s"]*n)), values in
  params. LIKE inputs: escape % and _ before wrapping in wildcards.
- .mea header values are DATA, never code: parsed with type coercion,
  inserted via placeholders like any user input (a crafted .mea file is
  an injection vector too).
Verification: grep the codebase for f-string/format/concat SQL — the only
hits allowed are whitelist-mapped identifiers.

Audit trail: audit_log table (schema §12) — every admin modification
records actor (OS username), action, target, old/new values. Supports
post-incident forensics and mistake rollback. Viewer has no grant on it.

Defense-in-depth summary (with where each layer is recorded):
network isolation & tunnels (§7) / keyring credentials (§6b) /
least-privilege dual accounts + no-DDL (§8) / parameterized SQL (§8) /
audit trail (§8) / optional TLS in LAN: ssl-mode=REQUIRED, MySQL
self-signed works out of the box (§7).

## 9. Search result pagination (RECORDED DECISION)

Row-size math: a result row (id, name, timestamp, type, category label,
truncated description) is 200-500 bytes -> 10k rows = 2-5 MB. A desktop
app can therefore afford client-side paging, which beats per-page
querying on UX (instant page jumps, in-memory re-sort/filter).

Chosen: hybrid C+B —
- COUNT(*) first; if total > HARD_CAP (20,000) do NOT fetch: UI shows
  "N results — narrow your filters" (guidance beats 400 pages).
- Otherwise fetch ALL matching light rows in one query (NEVER selecting
  header_json / note full text / any blob; description via LEFT(...,80)),
  ORDER BY measured_at DESC, mea_id DESC (mea_id as tiebreaker for a
  stable, deterministic order). Tk keeps rows in memory; Treeview renders
  PAGE_SIZE=50 per page; page jumps, column re-sort, secondary filtering
  are all zero-round-trip.
- Query runs in a worker thread (existing rule) with a busy indicator.
- Composite index (measured_at, mea_id) created now; if the library ever
  outgrows client-side paging (>100k rows), switch to keyset pagination
  WHERE (measured_at, mea_id) < (:last_dt, :last_id) — sort key and index
  are already in place, no schema change needed.
- Thumbnails are never preloaded with the list; if inline thumbnails are
  added later, load asynchronously for the CURRENT page only and cancel
  pending loads on page change.

## 2b. Cross-validation against file TYPE 2 (RECORDED FINDINGS)

File type 2: 260826_123948_TEA_1_碧螺春_1.mea — different instrument
(serial 5H4-00615, drift tube 53000 um / 2700 V), firmware 4.82 (vs 2.52),
different method (TEA-C, 21 ms sweep, 3150 drift pts, 14289 spectra,
30 min run), real sample (not blank). 68 header keys vs 60.


### Header/binary split method (RECORDED, validated on both files)
Executed in PYTHON (ingest pipeline), never in the database. Division of
labor principle: all parsing/validation/matrix math/derivation lives in
Python (mea_parser.py as a pure, DB-free function split_mea(raw) ->
(header_dict, matrix) — unit-testable against sample files, and REUSED by
the viewer's full-resolution path so read/write sides can never drift);
MySQL does storage/indexing/search/grants/transactions/audit. Sole
in-DB algorithm: move_category's anti-cycle check, because it operates on
in-DB data and must be atomic with its UPDATE. Everything else — including
header_key_registry bookkeeping — is Python inside the ingest transaction
(a trigger COULD do the registry upsert via JSON_TABLE, but registry
bookkeeping is business flow, not an integrity constraint; the dividing
rule: only rules that must survive someone bypassing the app and writing
SQL directly belong in the DB layer).
PRIMARY: arithmetic back-calculation — boundary = file_size -
(Chunks count x Chunk sample count x 2). The two integers are regex'd from
the leading text region without needing the boundary first. Content-blind:
immune to binary data that happens to look printable.
SECONDARY (cross-check only): first non-printable byte. The two methods
must agree within a few bytes (observed delta = 1, the final newline);
disagreement => ingest_log 'parse_error', human review — never guess.
Failure coverage: printable-looking binary start breaks only the
heuristic (arith wins); a hypothetical trailing footer breaks only the
arithmetic (heuristic flags it). Validated byte-exact on fw2.52 and
fw4.82 files.

Reference implementation (mea_parser.py must follow this exactly):

```python
import re
import numpy as np

PRINTABLE = set(range(32, 256)) | {9, 10, 13}   # latin-1 text + \t \n \r

def split_mea(raw: bytes):
    """Split a .mea into (header_dict, int16 matrix).
    Raises MeaParseError on any inconsistency — never guesses."""
    # -- primary: arithmetic back-calculation (content-blind) --
    head_txt = raw[:200_000].decode('latin-1', errors='replace')
    m1 = re.search(r'Chunks count\s*=\s*(\d+)', head_txt)
    m2 = re.search(r'Chunk sample count\s*=\s*(\d+)', head_txt)
    if not (m1 and m2):
        raise MeaParseError('geometry keys not found in leading text')
    n_spec, n_drift = int(m1.group(1)), int(m2.group(1))
    boundary = len(raw) - n_spec * n_drift * 2      # header byte length
    if boundary <= 0:
        raise MeaParseError('declared matrix larger than file')

    # -- secondary: heuristic cross-check --
    heur = next(i for i, b in enumerate(raw[:300_000]) if b not in PRINTABLE)
    if abs(boundary - (heur + 1)) > 4:
        raise MeaParseError(
            f'boundary mismatch: arith={boundary} heuristic={heur}')

    # -- header: latin-1 key=value lines --
    header = {}
    for line in raw[:boundary].decode('latin-1').split('\n'):
        if '=' in line:
            k, v = line.split('=', 1)
            header[k.strip()] = v.strip()

    # -- matrix: int16 LE, shape check is implicit in reshape --
    matrix = np.frombuffer(raw[boundary:], dtype='<i2').reshape(n_spec, n_drift)
    return header, matrix
```

Notes: (a) reshape doubles as the byte-exactness assertion — any off-by-N
raises; (b) function is pure and DB-free by design (see division-of-labor
above); (c) unit tests pin it to both sample files: boundaries 5541/5993,
shapes (8571,4500)/(14289,3150), key counts 60/68.

CONFIRMED generalizations (no change needed):
- Header boundary = first non-printable byte: works on both.
- int16 LE matrix, shape from Chunks count x Chunk sample count: byte-exact
  on both files. Per-file geometry columns vindicated (21 vs 30 ms sweep).
- RIP detection via argmax|column mean|: works (idx 679 vs 1216).
- header_json-for-everything + header_key_registry: vindicated hard —
  21 new keys appeared (Timezone, Status comment, Snapshot, Sample loop *,
  Device Maintenance, Sensor drift voltage, ...), 13 keys disappeared.
  A fixed-column header schema would have rejected this file.
- instrument as its own table: constants genuinely differ per unit.
- run_telemetry long format: still 5 series, ~101 pts @ 21 s (interval
  itself changed 18s->21s — read from header_json, never hard-code).

CORRECTIONS applied to schema after this check:
1. Signal polarity is PER-FILE, not global: fw2.52 file is negative-going
   (RIP -464), fw4.82 file is positive-going (RIP +3979, max +5519).
   Added measurement.polarity ENUM('positive','negative'), detected at
   ingest from sign of RIP column mean. Rendering uses it (abs/invert).
2. Firmware 4.x RENAMED the telemetry keys with explicit semantics:
   'Flow Epc 1'->'Flow EPC IMS', 'Flow Epc 2'->'Flow EPC GC',
   'Pressure Epc 1/2'->'Pressure EPC IMS/GC'; setpoints
   'Flow1/Flow2 setpoint'->'Flow IMS/GC setpoint';
   'EPC ambient pressure'->'Start ambient pressure'.
   run_telemetry.series ENUM changed to canonical semantic values
   (flow_ims, flow_gc, press_ims, press_gc, press_ambient);
   gc_method columns renamed flow_ims/flow_gc_setpoint_ml_min.
   Ingest maintains a HEADER_KEY_ALIASES map {firmware key -> canonical
   target}; unknown variants fall through to header_json + registry as
   designed. Old Epc1=IMS(drift), Epc2=GC(carrier) equivalence verified
   by matching values (1500 / 2.0 ml-min ramp) across both files.
3. New Status value observed: "doubtful"; Class can be deeper
   ("program/pos/unprocessed") — both fine as VARCHAR, noted for
   sample_type classification rules.
Remaining caution: two files cover two firmware generations of one
machine type. BreathSpec / other G.A.S. models may differ further —
the registry will surface it.

## 2c. File TYPE 3 cross-check: fw 2.16, 2014 (RECORDED FINDINGS)

File type 3: 141023_121632.mea — same machine type, serial 1H1-00044,
firmware 2.16 ("Apr 25 2014"), 51 header keys, matrix 5762x4500,
20_MIN_JUICE program, polarity POSITIVE (so across three generations:
2.16 positive, 2.52 negative, 4.82 positive — per-file detection is
definitively required, no firmware-level rule exists).

split_mea() reference implementation passed UNCHANGED (byte-exact).
Schema DDL required ZERO changes — nullable promoted columns absorbed
everything. Three ingest-leniency rules recorded instead:

1. ANY promoted key may be absent: this generation lacks GC Column,
   Drift Gas, nom Drift Potential Difference/Tube Length, Sensor data
   entirely. Ingest uses header.get(k) semantics -> NULL, never an error.
   gc_method.program_hash computation must tolerate None components.
2. Telemetry series COUNT varies: only Flow Epc 1/2 exist here (no
   Pressure Epc 1/2, no Pressure Ambient). Ingest iterates the alias map
   with per-key existence checks; long-format run_telemetry absorbs any
   subset. Flow record interval varies again (12.1 s) — always read from
   header_json, never hard-code.
3. Value-format drift: Firmware date "Apr 25 2014" vs ISO "2017-10-06"
   (try multiple date formats, else NULL — raw stays in header_json);
   '"off" [°C]' quoted vs unquoted 'off'; 'xxx' placeholders. Numeric
   promotion rule: regex-extract the number, else NULL.
New keys registered: 'Sample number', 'Sensor ID' (empty-string value).
Coverage now: three firmware generations (2.16/2.52/4.82) of FlavourSpec.

## 10. Machine-derived vs human-entered fields (RECORDED DECISION)

Strict separation:
- MACHINE-DERIVED (written by ingest, may be regenerated by re-running
  the pipeline): all header-promoted columns, geometry, polarity, RIP,
  header_json, run_telemetry, previews, hashes.
- HUMAN-ENTERED (written only via admin-app UI, NEVER touched by ingest):
  description, note, category_id, and any future annotation fields.
Ingest leaves human fields NULL at import; re-ingest/reprocessing must
never overwrite them. This guarantees pipeline upgrades can safely
re-derive everything mechanical without destroying curation work.

Admin-app requirement — batch annotate: user multi-selects rows in the
list, opens "batch edit", enters description / note-append / category
once, applies to all selected (each row still audit-logged individually
with actor+old/new). Motivation: legacy files (e.g. fw2.16 era) carry
sparse headers; batch-of-30 identical context should not cost 30 manual
entries. This is a manual bulk-edit tool, NOT ingest auto-fill — content
always originates from a human.

## 2d. File TYPE 4: polarity has NO header indicator (RECORDED)

File type 4: 250814_152456_F_3.mea (bamboo shoot, 2025): SAME instrument
(1H1-00088), SAME fw 2.52, IDENTICAL 60-key set, identical Sensor
fields, Class "program/pos" in BOTH — yet this file is positive-going
(RIP +855) while the fish blank on the same machine is negative-going
(RIP -464). Full field-by-field diff found NO header field that
distinguishes them. Conclusion (final): polarity is undeterminable from
the header at ANY granularity; sign-of-RIP-column-mean detection from
the matrix is the only method. split_mea() and schema passed unchanged
(4th consecutive file TYPE validated, 2nd with zero changes needed;
future types 5, 6, ... follow the same cross-validation procedure).

## 11. Searching header keys (query patterns + UI design)

Three tiers, by where the key lives:
1. Promoted columns: ordinary indexed SQL — covers ~95% of daily search
   (name, date range, category subtree, sample_type, polarity).
2. header_json keys: JSON path operators —
   WHERE header_json->>'$."Filter"' = '"SG8"'
   Syntax notes: keys with spaces need $."quoted names"; header values
   often carry their own embedded quotes ('"SG8"'); numeric comparison =
   REGEXP_SUBSTR the number out of '35 [°C]' then CAST. These WHEREs are
   full-table scans — fine at thousands of rows (ms); if a JSON key
   becomes a daily filter, upgrade path A = functional index (no schema
   change), path B = promote to a real column + one UPDATE backfill.
3. run_telemetry values: ordinary SQL on the long table.

Search-UI structure:
- Main search bar: promoted-column filters only (fast path).
- Advanced "header condition" search: key DROPDOWN populated from
  header_key_registry (ORDER BY occurrences DESC) + value input.
  The registry doubles as the searchable-key catalog — whatever keys
  exist in the library appear automatically, nothing hard-coded.
  Injection safety: chosen key must exist in the registry (whitelist),
  value goes through placeholders as always.
- Missing-key semantics (important for understanding): ONE measurement
  table, one row per .mea, each row's header_json differs. A JSON-key
  search scans all rows; ->> on a row lacking that key yields NULL,
  which matches nothing — rows without the key silently and CORRECTLY
  fall out of the result. Users never need to know which files carry
  which keys; the query IS the filter. UX aid: the key dropdown shows
  occurrences vs total (e.g. "Timezone (1/4 files)") so sparse-key
  results are expected, not surprising.

## 12. Promotion criteria: column vs JSON (RECORDED DECISION)

Decision ladder for every header key:
  Q1 will it appear in WHERE/ORDER BY/JOIN?  no -> JSON (most keys stop here)
  Q2 frequently (daily search path)?         rarely -> JSON (+functional
                                             index if it warms up)
  Q3 needs type semantics (date/number
     comparison, FK, ENUM) ?                 yes -> typed column

Four legitimate reasons to promote: (1) high-frequency search condition,
(2) type semantics needed, (3) referential integrity (FK to instrument /
gc_method), (4) program logic depends on it (geometry columns, polarity —
needed to re-slice the binary / render, independent of search).
None of the four -> JSON.

Anti-criteria (do NOT promote because):
- "looks important" — importance != searched-on (Sensor data stays JSON);
- "safer to promote" — the opposite: every column is a liability
  (alias maps across firmware renames, format-drift parsing, NOT NULL
  validation per generation), while JSON keys cost nothing. Evidence:
  the ~22 promoted keys required alias/rename/format handling across
  three firmware generations; the ~40 JSON-resident keys required zero
  changes.

The line is cheap to move (JSON->column = ALTER + backfill from
header_json; middle state = functional index), so default CONSERVATIVE:
when unsure, JSON first; let header_key_registry occurrences and real
usage justify promotion later. Data decides, not intuition.

Supplementary tests (added later in the session, folded in here):
- Code-dependency test: if ingest/app CODE computes or branches on a key
  (geometry, polarity), promote — JSON access in code paths costs parsing
  and hides typos until runtime.
- Constraint test: NOT NULL / UNIQUE / FK / ENUM attach only to real
  columns (file_hash, category_id).
Both reinforce the same outcome recorded above (~22 of ~130 keys, 17%).

## 13. Backward-compatibility rules for anticipated changes (RECORDED)

All four "known unknowns" (new telemetry series, negative-ion mode, other
instrument models, non-int16 dtypes) resolve via ADDITIVE, backward-
compatible changes — existing rows never modified:
- ENUM evolution rule: append new values at the TAIL only, never reorder
  or insert mid-list (MySQL stores ENUM ordinals; tail-append is instant
  DDL, reordering corrupts existing rows' meaning).
- New attributes: ADD COLUMN ... NULL only — old rows read NULL
  ("unrecorded then"), all existing queries unaffected. Never retype,
  re-semanticize, or drop columns.
- dtype evolution: matrix_dtype column pre-exists; split_mea grows a
  dtype->bytes map; the same parser serves old and new rows because
  every row is SELF-DESCRIBING (geometry, dtype, polarity, pipeline_ver
  travel with the row).
- Ultimate insurance: mea_file holds bit-perfect originals, machine-
  derived fields are regenerable (§10 protects human fields), so even a
  discovered parsing mistake is recoverable by re-derivation — derived
  data is disposable, original data is immutable.

## 14. Duplicate-import prevention (RECORDED)

Three layers, first present since schema v1:
1. Content fingerprint: file_hash = SHA-256 of ORIGINAL bytes, UNIQUE
   constraint. Judges content identity — renamed/re-pathed copies are
   caught; a re-measurement of the same specimen (different bytes) is
   correctly ADMITTED as new data.
2. Ingest: check-then-insert for a friendly message ("identical to
   mea_id=N imported <date>"), with the UNIQUE constraint as the
   race-proof backstop (concurrent imports physically cannot double-
   insert; IntegrityError on uk_hash -> logged as duplicate).
3. Batch UX: duplicates are NORMAL in batch imports — per-file
   isolation, batch never aborts, summary "170 added / 30 skipped
   (duplicate) / 0 failed" with per-file detail from ingest_log.
Alias decision: when an identical file arrives under a NEW NAME, the
new name is recorded in ingest_log.message only (option a). Upgrade to
a dedicated mea_alias table only if regulatory audit of aliases becomes
a requirement.
Deliberately NOT doing fuzzy dedup: near-identical files (two exports,
slightly different telemetry) are genuinely distinct records — both
enter; human judgment + audit-logged manual delete if one must go.

## 15. Soft-delete policy: retire, never delete (RECORDED DECISION)

Measurements are NEVER physically deleted. measurement gains:
retired (0/1, default 0, indexed), retired_at, retired_by,
retired_reason. Retire/unretire are ordinary admin-UI actions,
audit-logged like any modification.

Enforcement (three layers, consistent with §8 philosophy):
- GRANT level: gcims_admin has NO DELETE on measurement/dependents —
  physical deletion is impossible even with leaked credentials.
  DELETE remains only on sample_category (empty-category cleanup;
  RESTRICT FK protects non-empty ones).
- Query discipline: ALL search/list queries filter retired = 0 by
  default; admin UI has a "show retired" toggle (viewer UI does not).
- Consequences embraced: uk_hash still blocks re-importing a retired
  file's identical bytes — correct, the data is still IN the library,
  just retired; the remedy is unretire, not re-import. ON DELETE CASCADE
  chains on dependents become vestigial (nothing deletes) — kept as
  belt-and-suspenders for DBA-level maintenance only.
Rationale: regulatory alignment (data integrity — records are voided,
not destroyed), and the business narrative: the reference library only
ever grows; mistakes are annotated, not erased.

## 16. Accountability & identity: the hybrid model (RECORDED DECISION)

### Three logs already answer "who did what"
- audit_log: every admin modification (actor, action, old/new, when)
- ingest_log: every import attempt and outcome
- retired_by/at/reason: dedicated retire trail
Plus new access_log: HIGH-VALUE reads only — original-file downloads
and full-resolution fetches (searches/previews deliberately unlogged).
Business value: "data exfiltration is traceable."

### Hybrid identity model (chosen over app-managed logins)
Two layers, each doing what it does best:
- AUTHENTICATION + hard permission boundary: MySQL PER-PERSON accounts
  (admin_albert, anno_chen, view_wang...) + GRANTs in MySQL's own
  system tables. Unbypassable — enforced by the server even against
  someone connecting outside our apps. We never store or hash
  passwords ourselves (keyring per machine, MySQL verifies).
- IDENTITY + business attributes: gcims.app_user table
  (mysql_user PK, display_name, role admin/annotator/viewer, active).
  On connect the app resolves CURRENT_USER() -> app_user row:
  active=0 -> refuse entry (deactivating a person is a UI checkbox,
  no GRANT surgery); role gates which UI features appear.
Conflict rule: where the layers disagree, GRANT wins — effective
permission is the INTERSECTION of both.
Rejected alternative: app-level login over one shared high-privilege
account — bypassable (shared credential + direct SQL client nullifies
the app_user table) and makes password storage our liability.

### Role templates (in schema file)
admin: full CRUD minus DELETE (soft-delete policy) + move_category.
annotator (optional middle role): SELECT + column-limited UPDATE
  (description, note, category_id only — MySQL column-level grants).
viewer: SELECT minus bookkeeping tables + INSERT own access_log rows.
New person = one CREATE USER + GRANT block (DBA, scripted, ~1 min)
+ one app_user row (admin UI, daily-life operations live there).

### Known, accepted boundary
audit_log covers in-app actions. A person with a valid admin
credential connecting via a raw SQL client can modify without an
audit_log row (MySQL Community has no server-side audit plugin).
Mitigations: per-person accounts (connection identity is provable),
source-IP-restricted accounts, keyring-only credentials. Recorded as
a limitation, not silently assumed away.

## 17. Storage layout: whole file + ASCII derivatives, NO separate binary (RECORDED)

What is stored per .mea:
- mea_file.content: the ENTIRE original file (header+matrix, verbatim
  bytes, zstd) — the single complete copy.
- ASCII derivatives: parsed header -> promoted columns + header_json +
  run_telemetry (few KB; exists because ASCII is queried daily).
- Matrix derivative: mea_preview only (~370 KB downsample; serves 95%
  of viewing).
- A separate standalone binary copy: deliberately NOT stored.

Why no separate binary: it is 99.99% of the file, so storing it apart
~doubles space for a benefit that high-frequency paths never use
(search/preview don't touch it; full-resolution is low-frequency and a
1-3 s wait is acceptable). Split cost is already near zero at read
time: geometry columns (header_bytes/n_spectra/n_drift_points/
matrix_dtype) let the viewer do raw[header_bytes:] -> reshape with no
header re-parse; the real time is zstd decompression, unavoidable
either way. Two copies of the same bytes would also create a
consistency liability against the single-source-of-truth rule.

Display-layer addendum (RECORDED, revised after web-future argument):
mea_preview carries BOTH forms, serving different consumers:
- heatmap_png at NATIVE preview resolution (prev_cols x prev_rows,
  1:1 pixel:datapoint; measured 275 KB on a signal-rich real file):
  pre-rendered with fixed recipe (abs + log + percentile clip + fixed
  colormap). Detail-page display in Tk AND future web <img>: zero JS,
  zero server rendering, natively HTTP-cacheable. Sizing rationale
  (measured): upscaling (2x = 573 KB) adds zero information — never
  upscale; downscaling loses peaks. The resolution CEILING is the
  pooling factor chosen at ingest — if higher-fidelity previews are
  ever wanted, tune pool_rt/pool_dt and re-derive (pipeline_ver),
  don't inflate the PNG.
- thumb_png (~260 px wide, ~35 KB): list-wall layer. Web list pages at
  50 rows x native 275 KB = 14 MB (unacceptable on mobile); 50 x 35 KB
  = 1.7 MB. Tk on LAN tolerates either, but web is on the roadmap so
  the thumbnail layer is split out now.
  png_render_ver records the recipe version -> whole-library selective
  re-render stays cheap (§10 regenerable-fields principle).
- preview_npz: the INTERACTIVE layer — colormap/scale switching and
  cursor readout in any frontend that manipulates the data.
History note: PNG was initially rejected (npz-only) when evaluation was
anchored to the Tk-only architecture; the decision was revised when the
L4/web trajectory re-entered the picture. Both forms are derivatives —
disposable and regenerable from mea_file.

Storage philosophy (system-wide): the original file is the immutable
law of record; everything else is a disposable, regenerable view
optimized for a read pattern. Derivation depth is proportional to read
frequency: ASCII -> column-level; matrix -> preview/PNG-level only.


## 18. Project scope: centralized .mea archive; analysis is external (RECORDED DECISION)

The database's mission is fixed: the CENTRALIZED, searchable, protected
home of .mea measurements (store / search / preview / provenance /
access control). Peak detection, fingerprinting, and quality judgment
are NOT this system's job — they belong to external analysis pipelines
(e.g. the separate GC-IMS peak-processing effort) that act as READERS:
fetch mea_file originals or preview_npz through the normal viewer
path, compute in their own world, keep their own outputs.

Consequences:
- peak & compound tables remain in the schema as DORMANT reservations
  (empty tables cost nothing; a future write-back decision needs no
  migration). detector_ver etc. stay defined but unused.
- Ingest has NO peak step; nothing in this project blocks on algorithm
  development.
- Loose coupling: the archive and the analysis tooling evolve
  independently, joined only by the "fetch a measurement" interface.
- Business narrative alignment: L1 (archive) is THIS project's
  deliverable with a crisp acceptance boundary; L2/L3 build ON it as
  separate follow-on efforts.

### Awakening clause: writing compound results back (RECORDED)
The intended future IS write-back: once external analysis can identify
compounds in a mea, the FINAL results ("this measurement contains these
compounds") return to this database — so the library holds not just
raw measurements but their identified compound profiles, enabling the
golden query "which samples contain compound X".
Rules fixed NOW for that day:
1. Write-back is an EXPLICIT act of the analysis tool (admin-level
   account), never part of ingest — §18's boundary stands.
2. Results MUST carry a version/method identifier (analysis_ver):
   v1 and v2 identifications of the same mea coexist, comparable,
   never overwriting — algorithm progress is additive, not rewriting.
3. Results are machine-derived (§10: deletable, re-derivable);
   a future human confirmation ("analyst verified this ID") is a
   HUMAN field (confirmed_by) under the never-overwrite protection.
Granularity note: peak (per-peak, with compound FK) can carry this, or
a leaner mea_compound results table (mea_id, compound_id,
amount/score, analysis_ver) may be added then — final-result queries
prefer the latter; decide when the analysis output shape is real.

### Legacy coverage: retroactive appreciation (RECORDED)
Partial coverage (new measurements identified, legacy ones not) is the
NATURAL state, handled by the same absence-is-not-an-error semantics
as missing header keys — no special mechanism needed. UI shows an
identification badge ("identified v2 / 12 compounds" vs "not yet") and
a has-results filter.
More importantly: legacy means NOT-YET-identified, not UN-identifiable.
Because mea_file preserves complete originals, a matured pipeline can
backfill-identify the ENTIRE legacy library (§17 pattern) — a 2014
file can be identified by a 2027 algorithm. This is the whole-file
storage decision's largest long-term payoff: historical data
appreciates retroactively as algorithms improve. Honest caveat:
legacy identifications inherit sparse-metadata limits (fw2.16 lacks
column info -> weaker RI anchoring), so results carry analysis_ver +
confidence; curating annotations NOW (§10 batch edit) directly
improves FUTURE legacy identification quality.


## 19. Leniency boundary: tolerate missing data, never silent parser failure (RECORDED)

§2c leniency rules have an implicit boundary now made explicit:
- Data ABSENT (key missing from header): silently store NULL — the file
  genuinely lacks the information. program_name is NULL-able for this.
- Data PRESENT but our parser cannot interpret it (e.g. a future
  firmware reformats the Program string): NEVER silently degrade to a
  placeholder that mimics real data. Log an ingest warning
  (ingest_log result 'ok_with_warnings' — file still imported, nothing
  lost), use a visibly artificial marker ('(unparsed)') where a value
  is structurally required. Rationale: silent fallback would mass-
  produce garbage dictionary rows (e.g. hundreds of same-named methods
  with distinct hashes) and hide the "parser needs updating" signal —
  violating fail-loud. Recovery is always possible: raw strings are
  preserved (program_raw / header_json), so after a parser update one
  UPDATE backfills the affected rows.
Admin app: surface a "pending warnings" list from ingest_log.

## 20. Unified "Google-style" search with hit highlighting (RECORDED)

Search app's primary interface is ONE search box. User types a term
(e.g. "咖啡" / "coffee"); the app fans out ONE term to four sources and
merges results:

  A. Category tree: sample_category nodes whose name/full_label match ->
     expand each matched node's subtree (depth-capped recursive CTE) ->
     measurements attached anywhere below.
  B. Text columns: sample_name, description, note, folder_name via
     LIKE %term% (utf8mb4 ci collation = case-insensitive; % and _
     escaped per injection rules).
  C. Method names: gc_method.program_name LIKE %term% -> measurements
     using those methods.
  D. Header values (optional, advanced toggle; off by default for speed):
     JSON_SEARCH(header_json, 'all', '%term%') per row.

Merge: UNION of mea_id sets, deduplicated; each result row carries a
match_source set (category/name/note/method/...) shown as small badges.
Ranking: exact sample_name match > category match > name substring >
description/note > method > header. All queries parameterized; sources
B and C are single LIKE scans (ms at this scale); A is the standard
subtree CTE.

Hit highlighting:
- Result list (ttk.Treeview) cannot style substrings inside a cell.
  Chosen approach: the list shows match-source badges + the matched
  column's text; TRUE substring highlighting happens in the detail
  panel, which uses tk.Text widgets where tag_add on the matched ranges
  renders highlight (yellow background) for every occurrence in
  sample_name / description / note / header values.
- Implementation: after search, keep the term; detail panel calls
  text.search(term, idx, nocase=1) loop -> tag_add('hit', ...) with
  tag_configure('hit', background='#ffe58a').
- If richer in-list highlighting is ever required, the list can switch
  to a tk.Text-based row renderer or a web UI later — the search fan-out
  layer is UI-agnostic by design.

Multi-term input (space-separated): terms are ANDed across the row's
combined searchable text (each term must hit somewhere), consistent
with search-engine intuition.

Governance reminder (unchanged): category tagging quality at import time
determines source-A recall; the unified box makes poor tagging
survivable via B/C, not painless.
