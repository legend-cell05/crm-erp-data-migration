-- ===========================================================================
-- keystone's own state.
--
-- Four tables, each answering a question that is asked during a real
-- migration and cannot be answered from the source or the target alone:
--
--   run       what has been attempted, with which mapping version, and how
--             did it end?
--   crosswalk which legacy record became which target record? This is the
--             table that makes a re-run an update instead of a duplicate,
--             and the one whose loss would be unrecoverable.
--   reject    which records did not make it, and exactly why?
--   impact    what did the dry-run say would happen? A load refuses to start
--             without a matching row here.
--
-- Everything else can be rebuilt by running the migration again. These cannot.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS ${MIGRATION}.run (
    run_id              UUID PRIMARY KEY,
    kind                TEXT NOT NULL,
    mapping_version     TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'RUNNING',
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    records_read        BIGINT NOT NULL DEFAULT 0,
    records_mapped      BIGINT NOT NULL DEFAULT 0,
    records_written     BIGINT NOT NULL DEFAULT 0,
    records_rejected    BIGINT NOT NULL DEFAULT 0,
    notes               TEXT,
    CONSTRAINT ck_run_kind   CHECK (kind IN ('dry_run', 'load', 'retry', 'reconcile')),
    CONSTRAINT ck_run_status CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'BLOCKED')),
    CONSTRAINT ck_run_counts CHECK (
        records_read >= 0 AND records_mapped >= 0
        AND records_written >= 0 AND records_rejected >= 0
    )
);

CREATE INDEX IF NOT EXISTS ix_run_started ON ${MIGRATION}.run (started_at DESC);
CREATE INDEX IF NOT EXISTS ix_run_kind    ON ${MIGRATION}.run (kind, started_at DESC);

-- ---------------------------------------------------------------------------
-- The crosswalk.
--
-- Keyed on (entity, source_id) because that is the question asked on every
-- single record: "have I already migrated this one, and what did it become?"
--
-- `content_hash` is what turns the answer into three outcomes rather than
-- two: unknown source id -> create, known id with a new hash -> update,
-- known id with the same hash -> skip. Without it every re-run would rewrite
-- every record, which is slow, noisy in the target's audit log, and hides the
-- handful of records that genuinely changed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${MIGRATION}.crosswalk (
    entity              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    target_id           TEXT NOT NULL,
    alternate_key       TEXT NOT NULL,
    content_hash        CHAR(64) NOT NULL,
    first_loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_id         UUID REFERENCES ${MIGRATION}.run (run_id),
    load_count          INT NOT NULL DEFAULT 1,
    PRIMARY KEY (entity, source_id),
    CONSTRAINT ck_crosswalk_counts CHECK (load_count > 0)
);

-- The reverse lookup matters too: "this target record -- where did it come
-- from?" is the first question asked when a user reports something wrong.
CREATE UNIQUE INDEX IF NOT EXISTS ux_crosswalk_target
    ON ${MIGRATION}.crosswalk (entity, target_id);

-- ---------------------------------------------------------------------------
-- Rejects.
--
-- One row per rejected record, not one per failed attempt: a permanently
-- broken source record would otherwise fill this table with copies of the
-- same problem. `attempts` counts, `status` ends it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${MIGRATION}.reject (
    entity              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    stage               TEXT NOT NULL,
    field               TEXT,
    rule                TEXT,
    message             TEXT NOT NULL,
    payload             JSONB NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    attempts            INT NOT NULL DEFAULT 1,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_id         UUID REFERENCES ${MIGRATION}.run (run_id),
    PRIMARY KEY (entity, source_id),
    CONSTRAINT ck_reject_stage  CHECK (stage IN ('extract', 'map', 'validate', 'load')),
    CONSTRAINT ck_reject_status CHECK (status IN ('PENDING', 'RESOLVED', 'ABANDONED'))
);

CREATE INDEX IF NOT EXISTS ix_reject_status ON ${MIGRATION}.reject (status, entity);
CREATE INDEX IF NOT EXISTS ix_reject_rule   ON ${MIGRATION}.reject (entity, rule)
    WHERE status = 'PENDING';

-- ---------------------------------------------------------------------------
-- Dry-run impact.
--
-- The projection a dry-run produces, per entity and per action. The load gate
-- reads this table: no row for the current mapping version means no dry-run
-- was performed, and the load refuses to start.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${MIGRATION}.impact (
    run_id              UUID NOT NULL REFERENCES ${MIGRATION}.run (run_id) ON DELETE CASCADE,
    entity              TEXT NOT NULL,
    action              TEXT NOT NULL,
    record_count        BIGINT NOT NULL,
    PRIMARY KEY (run_id, entity, action),
    CONSTRAINT ck_impact_action CHECK (action IN ('create', 'update', 'skip', 'reject')),
    CONSTRAINT ck_impact_count  CHECK (record_count >= 0)
);

-- ---------------------------------------------------------------------------
-- Source profiling, kept per run so two dry-runs can be compared.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${MIGRATION}.profile (
    run_id              UUID NOT NULL REFERENCES ${MIGRATION}.run (run_id) ON DELETE CASCADE,
    entity              TEXT NOT NULL,
    column_name         TEXT NOT NULL,
    row_count           BIGINT NOT NULL,
    null_count          BIGINT NOT NULL,
    blank_count         BIGINT NOT NULL,
    distinct_count      BIGINT NOT NULL,
    sample_values       TEXT[],
    PRIMARY KEY (run_id, entity, column_name)
);
