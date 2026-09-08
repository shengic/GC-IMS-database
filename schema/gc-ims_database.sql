-- ============================================================
-- GC-IMS .mea measurement database (MySQL 8.0+)
-- Version: 1.0.0
-- Deployment variant: targets database `gc-ims_database` (with hyphen).
-- Identical to gcims_schema.sql (the canonical source) except for the
-- database name. Use this file to bootstrap the production DB directly;
-- use gcims_schema.sql via `scripts/apply_schema.py --database DB` to
-- deploy under any other name.
-- ============================================================

CREATE DATABASE IF NOT EXISTS `gc-ims_database`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `gc-ims_database`;

-- ------------------------------------------------------------
-- 1. Instrument registry (from header: Machine type/serial, ADIO, firmware)
-- ------------------------------------------------------------
CREATE TABLE instrument (
    instrument_id    SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    machine_type     VARCHAR(64)  NOT NULL,          -- "FlavourSpec®"
    machine_serial   VARCHAR(32)  NOT NULL,          -- "1H1-00088"
    machine_name     VARCHAR(64),                    -- "GAScontrol"
    adio_serial      VARCHAR(32),                    -- "ADIO-10033"
    firmware_version VARCHAR(16),                    -- "2.52"
    firmware_date    DATE,
    drift_tube_um    INT UNSIGNED,                   -- 98000 [µm]
    drift_voltage_v  DECIMAL(7,1),                   -- 5000 [V]
    sensor_data      VARCHAR(128),                   -- tritium source info
    UNIQUE KEY uk_serial (machine_serial)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- 2. GC method / program (from header: Program, GC Column, setpoints)
--    One row per distinct program; many measurements share it.
-- ------------------------------------------------------------
CREATE TABLE gc_method (
    method_id        SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    program_name     VARCHAR(64) NULL,               -- parsed Name=`...`; NULL if Program key absent;
                                                     -- '(unparsed)' + ingest warning if present but pattern unmatched
    program_raw      TEXT,                           -- full Program string
    gc_column        VARCHAR(128),                   -- "FS-SE54-CB-0.5, 15m x 0,32ID"
    drift_gas        VARCHAR(32),                    -- "nitrogen"
    flow_ims_setpoint_ml_min DECIMAL(6,1),           -- drift gas 150.0
    flow_gc_setpoint_ml_min  DECIMAL(6,1),           -- carrier 2.0
    temp_setpoints_c JSON,                           -- {"T1":45,"T2":60,...,"T6":"off"}
    program_hash     CHAR(64) NOT NULL,              -- sha256(program_raw + column + gas)
    UNIQUE KEY uk_program (program_hash)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- 3. Measurement: one row per .mea (search hub; NO blobs here)
-- ------------------------------------------------------------
CREATE TABLE measurement (
    mea_id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    file_name        VARCHAR(255) NOT NULL,          -- "260210_095729_FISH_MEAT_BLANK.mea"
    file_hash        CHAR(64)     NOT NULL,          -- sha256 of original bytes
    file_size_bytes  BIGINT UNSIGNED NOT NULL,       -- 77144541
    measured_at      DATETIME     NOT NULL,          -- Timestamp "2026-02-10T09:57:29"
    instrument_id    SMALLINT UNSIGNED NOT NULL,
    method_id        SMALLINT UNSIGNED NOT NULL,
    sample_name      VARCHAR(255) NOT NULL,          -- Sample = "Fish meat blank"
    sample_class     VARCHAR(64),                    -- Class  = "program/pos"
    sample_type      ENUM('sample','blank','standard','qc','unknown')
                     NOT NULL DEFAULT 'unknown',     -- classified at ingest
    status           VARCHAR(16),                    -- header Status = "valid"
    -- matrix geometry (needed to reinterpret binary without reparsing header)
    header_bytes     INT UNSIGNED NOT NULL,          -- 5541
    n_spectra        INT UNSIGNED NOT NULL,          -- Chunks count = 8571
    n_drift_points   INT UNSIGNED NOT NULL,          -- Chunk sample count = 4500
    matrix_dtype     VARCHAR(8) NOT NULL DEFAULT 'int16le',
    chunk_averages   SMALLINT UNSIGNED,              -- 6
    sample_rate_khz  DECIMAL(7,2),                   -- 150
    trig_repetition_ms DECIMAL(7,3),                 -- 30  (drift sweep length)
    run_time_s       DECIMAL(8,1),                   -- derived: 8571*0.030*6 = 1542.8
    -- environment snapshot (search/QA relevant)
    ambient_pressure_kpa DECIMAL(7,3),               -- 'EPC ambient pressure' (fw2.x) / 'Start ambient pressure' (fw4.x)
    polarity         ENUM('positive','negative') NULL, -- sign of RIP column mean, DETECTED PER FILE at ingest
                                                     -- (verified: fw2.52 file negative, fw4.82 file positive)
    -- RIP position found at ingest (drift index & ms), for normalized search
    rip_drift_index  INT UNSIGNED,                   -- e.g. 1216
    rip_drift_ms     DECIMAL(8,4),
    -- everything else from the 60-key header, verbatim
    header_json      JSON NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_hash (file_hash),
    KEY idx_sample   (sample_name),
    KEY idx_time_id  (measured_at, mea_id),
    KEY idx_type_time (sample_type, measured_at),
    KEY idx_method   (method_id),
    CONSTRAINT fk_meas_inst   FOREIGN KEY (instrument_id) REFERENCES instrument(instrument_id),
    CONSTRAINT fk_meas_method FOREIGN KEY (method_id)     REFERENCES gc_method(method_id)
) ENGINE=InnoDB;

-- Optional: functional index example for a JSON header field you later
-- decide to search on, without schema change:
-- ALTER TABLE measurement ADD INDEX idx_filter
--   ((CAST(header_json->>'$."Filter"' AS CHAR(16))));

-- ------------------------------------------------------------
-- 4. In-run telemetry series (Flow Epc 1/2, Pressure Epc 1/2, Ambient)
--    Header stores them as space-separated arrays @ 18 s interval.
--    Long format; small (~85 rows per series per run at 25.7 min).
-- ------------------------------------------------------------
CREATE TABLE run_telemetry (
    mea_id     BIGINT UNSIGNED NOT NULL,
    -- canonical semantic names; ingest maps firmware-specific header keys
    -- to these via HEADER_KEY_ALIASES (fw<=2.x: 'Flow Epc 1'->flow_ims,
    -- 'Flow Epc 2'->flow_gc; fw>=4.x: 'Flow EPC IMS'/'Flow EPC GC', etc.)
    -- pump1_* added for GC-IMS product line (5F1-xxxxx, fw 4.73) which
    -- controls GC flow with a pump instead of a second EPC; see §2f.
    -- ENUM values appended at TAIL only per §13 backward-compat rule.
    series     ENUM('flow_ims','flow_gc','press_ims','press_gc','press_ambient',
                    'pump1_flow','pump1_pressure') NOT NULL,
    seq_no     SMALLINT UNSIGNED NOT NULL,           -- 0,1,2,... (t = seq_no * record_interval)
    value      DECIMAL(10,3) NOT NULL,
    PRIMARY KEY (mea_id, series, seq_no),
    CONSTRAINT fk_tel FOREIGN KEY (mea_id) REFERENCES measurement(mea_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- 5. Peak table (populated by your peak-picking step; search core)
-- ------------------------------------------------------------
-- ============ DORMANT (out of project scope, RECORDED §18) ============
-- compound & peak are RESERVED for a possible future analysis layer.
-- This project's mission is the centralized .mea archive; peak
-- detection/matching lives in EXTERNAL analysis pipelines that CONSUME
-- from this DB (fetch mea_file / preview_npz) and keep their own
-- outputs. These tables stay empty; ingest never writes them.
-- Kept because empty tables cost nothing and avoid a future migration
-- if write-back is ever decided.
CREATE TABLE compound (
    compound_id    INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    name           VARCHAR(255) NOT NULL,
    cas_number     VARCHAR(20),
    formula        VARCHAR(64),
    ri             DECIMAL(7,1),                     -- retention index
    dt_rip_rel_ref DECIMAL(8,5),                     -- reference drift time / RIP
    UNIQUE KEY uk_cas (cas_number),
    KEY idx_name (name)
) ENGINE=InnoDB;

CREATE TABLE peak (
    peak_id        BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    mea_id         BIGINT UNSIGNED NOT NULL,
    rt_s           DECIMAL(8,2) NOT NULL,            -- retention time [s]
    dt_ms          DECIMAL(8,4) NOT NULL,            -- absolute drift time [ms]
    dt_rip_rel     DECIMAL(8,5) NOT NULL,            -- dt / dt_RIP  (cross-run comparable)
    intensity      DOUBLE NOT NULL,
    volume         DOUBLE,
    fwhm_rt_s      DECIMAL(7,2),
    fwhm_dt_ms     DECIMAL(7,4),
    ion_form       ENUM('monomer','dimer','trimer','unknown') DEFAULT 'unknown',
    detector_ver   SMALLINT UNSIGNED NOT NULL DEFAULT 0,  -- peak-detection algorithm
                                                          -- version; 0 = placeholder.
                                                          -- Peaks are machine-derived
                                                          -- (§10): delete+re-derive per
                                                          -- version is allowed — the
                                                          -- soft-delete policy governs
                                                          -- measurement, NOT peak.
    compound_id    INT UNSIGNED NULL,
    CONSTRAINT fk_peak_mea  FOREIGN KEY (mea_id)      REFERENCES measurement(mea_id) ON DELETE CASCADE,
    CONSTRAINT fk_peak_cmpd FOREIGN KEY (compound_id) REFERENCES compound(compound_id),
    KEY idx_window (rt_s, dt_rip_rel),               -- "peaks in rt±Δ, dtrel±Δ" search
    KEY idx_cmpd   (compound_id, mea_id)             -- "which samples contain compound X"
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- 6. Downsampled preview (npz blob; quick-view heatmap source)
--    max-pool downsampled; ~700x750 float32 -> ~370 KB compressed
-- ------------------------------------------------------------
CREATE TABLE mea_preview (
    mea_id       BIGINT UNSIGNED PRIMARY KEY,
    preview_npz  MEDIUMBLOB NOT NULL,                -- np.savez_compressed:
                                                     --   matrix (prev_rows x prev_cols f32)
                                                     --   rt_axis_s, dt_axis_ms
                                                     -- serves INTERACTIVE viewing (colormap/
                                                     -- scale switching, cursor readout)
    heatmap_png  MEDIUMBLOB NULL,                    -- pre-rendered heatmap at NATIVE
                                                     -- preview resolution (prev_cols x
                                                     -- prev_rows, 1:1 pixel:datapoint,
                                                     -- ~150-300 KB; abs+log+percentile
                                                     -- clip, fixed colormap). Detail-page
                                                     -- display; future web <img> direct.
                                                     -- Never upscaled (no info gain);
                                                     -- ceiling = pooling factors at ingest
                                                     -- (tune pool_rt/pool_dt + pipeline_ver
                                                     -- to re-derive larger if ever needed).
    thumb_png    MEDIUMBLOB NULL,                    -- ~260px-wide thumbnail (~35 KB) for
                                                     -- LIST WALLS (50/page x 275 KB native
                                                     -- would be 14 MB on web/mobile; 50 x
                                                     -- 35 KB is healthy).
    png_render_ver SMALLINT UNSIGNED NULL,           -- render-recipe version; bump to
                                                     -- selectively regenerate all PNGs
    prev_rows    SMALLINT UNSIGNED NOT NULL,
    prev_cols    SMALLINT UNSIGNED NOT NULL,
    pool_rt      SMALLINT UNSIGNED NOT NULL,         -- pooling factors used (e.g. 12)
    pool_dt      SMALLINT UNSIGNED NOT NULL,         -- (e.g. 6)
    pipeline_ver SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    CONSTRAINT fk_prev FOREIGN KEY (mea_id) REFERENCES measurement(mea_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- 7. Whole original .mea (cold archive; single source of truth)
--    77.1 MB -> ~18.9 MB @ zstd level 3 (measured on real file)
-- ------------------------------------------------------------
CREATE TABLE mea_file (
    mea_id       BIGINT UNSIGNED PRIMARY KEY,
    compression  ENUM('none','zstd','gzip') NOT NULL DEFAULT 'zstd',
    orig_size    BIGINT UNSIGNED NOT NULL,
    stored_size  BIGINT UNSIGNED NOT NULL,
    content      LONGBLOB NOT NULL,                  -- zstd-compressed original bytes
    stored_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_file FOREIGN KEY (mea_id) REFERENCES measurement(mea_id) ON DELETE CASCADE
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

-- ------------------------------------------------------------
-- 8. Sample category tree (multi-level, e.g. 酒類 > Whisky > Single Malt > brand)
--    Adjacency list + recursive CTE (MySQL 8). Measurements may attach
--    to ANY level via measurement.category_id.
--    ANTI-CYCLE STRATEGY (three layers, see docs/DESIGN.md §7):
--      a) triggers block self-parenting (cheapest degenerate case)
--      b) move_category() procedure is the ONLY sanctioned way to change
--         parent_id — it runs a full subtree check before updating
--      c) all subtree queries carry a depth cap (lvl < 10) so even
--         corrupted data cannot hang a query
-- ------------------------------------------------------------
CREATE TABLE sample_category (
    category_id  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    parent_id    INT UNSIGNED NULL,                 -- NULL = root
    name         VARCHAR(128) NOT NULL,
    full_label   VARCHAR(512) NOT NULL,             -- materialized path "酒類 > Whisky > ..."
    depth        SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    sort_order   SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    CONSTRAINT fk_cat_parent FOREIGN KEY (parent_id)
        REFERENCES sample_category(category_id) ON DELETE RESTRICT,
    UNIQUE KEY uk_sibling (parent_id, name),
    KEY idx_parent (parent_id)
) ENGINE=InnoDB;

-- Anti-cycle layer (a): triggers — block self-parenting.
-- MySQL triggers CANNOT query the table being modified (error 1442),
-- so full multi-hop cycle detection is impossible here; that lives in
-- move_category() below. These triggers are the cheap safety net that
-- also catches manual SQL-client mistakes.
DELIMITER //
CREATE TRIGGER trg_cat_ins_no_self
BEFORE INSERT ON sample_category
FOR EACH ROW
BEGIN
    IF NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.category_id THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'sample_category: node cannot be its own parent';
    END IF;
END//
CREATE TRIGGER trg_cat_upd_no_self
BEFORE UPDATE ON sample_category
FOR EACH ROW
BEGIN
    IF NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.category_id THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'sample_category: node cannot be its own parent';
    END IF;
END//

-- Anti-cycle layer (b): the ONLY sanctioned way to re-parent a node.
-- Full subtree check: rejects if new parent is the node itself or any
-- descendant. App code must call this procedure, never raw UPDATE parent_id.
-- Requires MySQL 8.0.19+ (CTE inside procedures).
CREATE PROCEDURE move_category(IN p_node INT UNSIGNED,
                               IN p_new_parent INT UNSIGNED)
BEGIN
    DECLARE v_cycle INT DEFAULT 0;
    IF p_new_parent IS NOT NULL THEN
        IF p_node = p_new_parent THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'move_category: node cannot be its own parent';
        END IF;
        WITH RECURSIVE subtree AS (
            SELECT category_id, 0 AS lvl
            FROM sample_category WHERE category_id = p_node
            UNION ALL
            SELECT c.category_id, s.lvl + 1
            FROM sample_category c JOIN subtree s ON c.parent_id = s.category_id
            WHERE s.lvl < 10
        )
        SELECT COUNT(*) INTO v_cycle FROM subtree WHERE category_id = p_new_parent;
        IF v_cycle > 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'move_category: target parent is inside node subtree (cycle)';
        END IF;
    END IF;
    UPDATE sample_category SET parent_id = p_new_parent
    WHERE category_id = p_node;
    -- NOTE: caller must rebuild full_label/depth for the moved subtree
END//
DELIMITER ;

-- Anti-cycle layer (c): query pattern — every subtree traversal uses a
-- depth cap. Canonical query ("all measurements under category :root
-- including descendants"):
--   WITH RECURSIVE subtree AS (
--       SELECT category_id, 0 AS lvl FROM sample_category WHERE category_id = :root
--       UNION ALL
--       SELECT c.category_id, s.lvl + 1
--       FROM sample_category c JOIN subtree s ON c.parent_id = s.category_id
--       WHERE s.lvl < 10)
--   SELECT m.* FROM measurement m JOIN subtree st USING (category_id);
-- Also SET SESSION cte_max_recursion_depth = 32; as a fuse.
-- Nightly integrity check (detects pre-existing cycles):
--   WITH RECURSIVE walk AS (
--       SELECT category_id AS start_id, parent_id AS cur_id, 1 AS steps
--       FROM sample_category WHERE parent_id IS NOT NULL
--       UNION ALL
--       SELECT w.start_id, c.parent_id, w.steps + 1
--       FROM walk w JOIN sample_category c ON c.category_id = w.cur_id
--       WHERE c.parent_id IS NOT NULL AND w.steps < 32)
--   SELECT DISTINCT start_id FROM walk WHERE cur_id = start_id;

-- Provenance / annotation / category columns on measurement
ALTER TABLE measurement
    ADD COLUMN full_path   VARCHAR(1024) NULL,
    ADD COLUMN folder_name VARCHAR(255)  NULL,
    ADD COLUMN description VARCHAR(512)  NULL,
    ADD COLUMN note        TEXT          NULL,
    ADD COLUMN category_id INT UNSIGNED  NULL,
    ADD KEY idx_folder (folder_name),
    ADD KEY idx_category (category_id),
    ADD CONSTRAINT fk_meas_category FOREIGN KEY (category_id)
        REFERENCES sample_category(category_id) ON DELETE SET NULL;

-- Soft-delete policy (RECORDED DECISION): measurements are NEVER
-- physically deleted — only retired. Search/list queries MUST filter
-- retired = 0 by default; admin UI offers "show retired" toggle.
-- Retiring/unretiring goes to audit_log like any admin modification.
ALTER TABLE measurement
    ADD COLUMN retired        TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN retired_at     DATETIME NULL,
    ADD COLUMN retired_by     VARCHAR(64) NULL,
    ADD COLUMN retired_reason VARCHAR(255) NULL,
    ADD KEY idx_retired (retired);
-- Optional Chinese-capable fulltext on note:
--   ALTER TABLE measurement ADD FULLTEXT KEY ft_note (note) WITH PARSER ngram;

-- ------------------------------------------------------------
-- 9. Integrity CHECK constraints on `measurement`.
-- Fail-fast column-scope invariants — no need to wait for the nightly
-- integrity scan (tests/test_integrity_scan.py) to catch these. Kept as
-- CHECK (not triggers) because they are pure per-row math / lookups and
-- MySQL 8.0.16+ enforces them automatically on INSERT and UPDATE.
-- Content-level invariants (SHA-256 roundtrip, PNG validity, npz shape)
-- stay in the pytest scan — MySQL cannot express them without external
-- libraries and they detect *storage* corruption, not app bugs.
-- ------------------------------------------------------------
ALTER TABLE measurement
    ADD CONSTRAINT ck_meas_geometry
        CHECK (file_size_bytes = header_bytes + n_spectra * n_drift_points * 2),
    ADD CONSTRAINT ck_meas_retired_consistency
        CHECK ((retired = 0 AND retired_at IS NULL AND retired_by IS NULL)
            OR (retired = 1 AND retired_at IS NOT NULL AND retired_by IS NOT NULL)),
    ADD CONSTRAINT ck_meas_rip_index_within_axis
        CHECK (rip_drift_index IS NULL OR rip_drift_index < n_drift_points),
    ADD CONSTRAINT ck_meas_matrix_dtype
        CHECK (matrix_dtype IN ('int16le'));

-- ------------------------------------------------------------
-- 10. Header key registry: tracks every header key ever seen.
--     Ingest NEVER fails on unknown keys: all keys go to header_json,
--     new key names are recorded here for later disposition
--     (promote to typed column / explode to telemetry / leave in JSON).
-- ------------------------------------------------------------
CREATE TABLE header_key_registry (
    key_name      VARCHAR(128) PRIMARY KEY,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    first_mea_id  BIGINT UNSIGNED,
    sample_value  VARCHAR(255),
    occurrences   BIGINT UNSIGNED DEFAULT 1,
    disposition   ENUM('json_only','promoted','telemetry','ignored')
                  DEFAULT 'json_only'
) ENGINE=InnoDB;
-- Ingest upsert: INSERT ... ON DUPLICATE KEY UPDATE occurrences=occurrences+1

-- ------------------------------------------------------------
-- 11. Ingest audit (traceability / reprocessing bookkeeping)
-- ------------------------------------------------------------
CREATE TABLE ingest_log (
    log_id       BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    mea_id       BIGINT UNSIGNED,
    file_name    VARCHAR(255) NOT NULL,
    result       ENUM('ok','duplicate','parse_error','failed','ok_with_warnings') NOT NULL,
    message      VARCHAR(512),
    pipeline_ver SMALLINT UNSIGNED NOT NULL,
    ingested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_mea (mea_id)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- Recommended server settings (my.cnf), sized for this data:
--   max_allowed_packet      = 256M   (largest zstd blob + margin)
--   innodb_file_per_table   = ON
--   innodb_buffer_pool_size = as RAM allows; blob table pages are
--                             touched only on explicit download
-- Query discipline: list/search endpoints must never SELECT content
-- or preview_npz; fetch blobs only by single-row primary-key lookup.
-- ============================================================

-- ------------------------------------------------------------
-- 12. Identity layer (hybrid model, DESIGN.md §16):
--     AUTHENTICATION + hard permission boundary = MySQL per-person
--     accounts + GRANTs (mysql system tables, unbypassable).
--     IDENTITY + business attributes = app_user (this table):
--     display name, role for UI feature gating, active flag
--     (deactivate a person without touching GRANTs).
--     On connect, app resolves CURRENT_USER() -> app_user row;
--     active=0 -> refuse entry. Where the two layers disagree,
--     GRANT wins (security = intersection of both).
-- ------------------------------------------------------------
CREATE TABLE app_user (
    mysql_user   VARCHAR(32) PRIMARY KEY,   -- e.g. 'admin_albert'
    display_name VARCHAR(64) NOT NULL,
    role         ENUM('admin','annotator','viewer') NOT NULL,
    active       TINYINT(1) NOT NULL DEFAULT 1,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- High-value access trail: original-file downloads / full-resolution
-- fetches only (searches and previews are NOT logged).
CREATE TABLE access_log (
    access_id  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor      VARCHAR(32) NOT NULL,        -- mysql_user
    mea_id     BIGINT UNSIGNED NOT NULL,
    action     ENUM('download_original','full_resolution') NOT NULL,
    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_actor (actor, accessed_at),
    KEY idx_mea (mea_id)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- 13. Audit log: every admin-app modification records actor,
--     old/new values. Viewer account has NO grant on this table.
--     actor = mysql_user, cross-checkable against CURRENT_USER().
-- ------------------------------------------------------------
CREATE TABLE audit_log (
    audit_id    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor       VARCHAR(64) NOT NULL,
    action      VARCHAR(32) NOT NULL,
    target_tbl  VARCHAR(64) NOT NULL,
    target_id   BIGINT UNSIGNED NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    acted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_target (target_tbl, target_id)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- Application accounts (run as DBA; app accounts never get DDL/GRANT)
-- ------------------------------------------------------------
-- PER-PERSON accounts (hybrid identity model, DESIGN.md §16):
-- one MySQL account per human; role template below. After CREATE USER,
-- also INSERT the matching app_user row.
--
-- ADMIN template (per person, e.g. admin_albert):
--   Soft-delete policy at GRANT level: NO DELETE on measurement and
--   dependents; DELETE only on sample_category (empty-category cleanup).
--   CREATE USER 'admin_albert'@'192.168.1.%' IDENTIFIED BY '<keyring>';
--   GRANT SELECT, INSERT, UPDATE ON gcims.* TO 'admin_albert'@'192.168.1.%';
--   GRANT DELETE ON gcims.sample_category TO 'admin_albert'@'192.168.1.%';
--   GRANT EXECUTE ON PROCEDURE gcims.move_category TO 'admin_albert'@'192.168.1.%';
--   INSERT INTO app_user VALUES ('admin_albert','沈老師','admin',1,DEFAULT);
--
-- ANNOTATOR template (optional middle role: may annotate, not import/retire):
--   CREATE USER 'anno_chen'@'192.168.1.%' IDENTIFIED BY '<keyring>';
--   GRANT SELECT ON gcims.* TO 'anno_chen'@'192.168.1.%';
--   GRANT UPDATE (description, note, category_id) ON gcims.measurement
--     TO 'anno_chen'@'192.168.1.%';
--   GRANT INSERT ON gcims.audit_log TO 'anno_chen'@'192.168.1.%';
--   INSERT INTO app_user VALUES ('anno_chen','陳研究員','annotator',1,DEFAULT);
-- VIEWER template (read-only; no EXECUTE, no audit_log access;
--   may INSERT its own access_log rows and read app_user for login):
--   CREATE USER 'view_wang'@'192.168.1.%' IDENTIFIED BY '<keyring>';
--   GRANT INSERT ON gcims.access_log TO 'view_wang'@'192.168.1.%';
--   GRANT SELECT ON gcims.app_user   TO 'view_wang'@'192.168.1.%';
--   (legacy shared-account line kept for reference:)
--   CREATE USER 'gcims_viewer'@'192.168.1.%' IDENTIFIED BY '<keyring>';
--   GRANT SELECT ON gcims.measurement      TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.instrument       TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.gc_method        TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.peak             TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.compound         TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.sample_category  TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.run_telemetry    TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.mea_preview      TO 'gcims_viewer'@'192.168.1.%';
--   GRANT SELECT ON gcims.mea_file         TO 'gcims_viewer'@'192.168.1.%';
--   (deliberately NOT granted: ingest_log, header_key_registry, audit_log)
