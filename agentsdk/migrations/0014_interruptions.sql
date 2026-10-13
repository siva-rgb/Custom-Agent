-- FR-112 (M29): interruptions and their decisions, recorded before anything waits on one.
--
-- runs gains UNIQUE (run_id, tenant_id, project_id), the one change to an existing table, so
-- every new row references its run with its own tenant and project and no row can name
-- another tenant's or project's run (NFR-31). One interruption per call that required
-- approval; at most one decision per interruption, enforced here by the unique key, so the
-- database itself refuses a second decision; standing decisions ordered by seq, so the
-- latest wins without a tie (P4-D15). Nothing is keyed on (run_id, tool_call_id): a model
-- may reuse a call id across turns. Rows are written once and never updated or deleted.
--
-- execution_manifests.approval_timeout is NULL until M30 records it, and for a manifest
-- written before this migration.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'runs_tenancy_identity'
                   AND conrelid = 'runs'::regclass) THEN
        ALTER TABLE runs ADD CONSTRAINT runs_tenancy_identity UNIQUE (run_id, tenant_id, project_id);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS interruptions (
    interruption_id UUID        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    run_id          UUID        NOT NULL,
    top_run_id      UUID        NOT NULL,
    node_id         TEXT,
    kind            TEXT        NOT NULL CHECK (kind = 'tool_approval'),
    tool_call_id    TEXT        NOT NULL,
    tool_name       TEXT        NOT NULL,
    agent_id        TEXT        NOT NULL,
    trigger         TEXT        NOT NULL CHECK (trigger IN ('policy', 'predicate', 'hook')),
    request         JSONB       NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    created_at      TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    CONSTRAINT interruptions_tenancy_identity UNIQUE (interruption_id, tenant_id, project_id),
    FOREIGN KEY (run_id, tenant_id, project_id) REFERENCES runs (run_id, tenant_id, project_id),
    FOREIGN KEY (top_run_id, tenant_id, project_id) REFERENCES runs (run_id, tenant_id, project_id)
);

CREATE INDEX IF NOT EXISTS interruptions_run ON interruptions (tenant_id, project_id, run_id);
CREATE INDEX IF NOT EXISTS interruptions_top ON interruptions (tenant_id, project_id, top_run_id);

CREATE TABLE IF NOT EXISTS standing_decisions (
    standing_id UUID        PRIMARY KEY,
    seq         BIGSERIAL   NOT NULL UNIQUE,
    tenant_id   TEXT        NOT NULL,
    project_id  TEXT        NOT NULL,
    run_id      UUID        NOT NULL,
    agent_id    TEXT        NOT NULL,
    tool_name   TEXT        NOT NULL,
    decision    TEXT        NOT NULL CHECK (decision IN ('approve', 'reject')),
    message     TEXT,
    principal   JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (run_id, tenant_id, project_id) REFERENCES runs (run_id, tenant_id, project_id)
);

CREATE INDEX IF NOT EXISTS standing_decisions_run ON standing_decisions (tenant_id, project_id, run_id, seq);

CREATE TABLE IF NOT EXISTS interruption_decisions (
    decision_id     UUID        PRIMARY KEY,
    interruption_id UUID        NOT NULL UNIQUE,
    tenant_id       TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    decision        TEXT        NOT NULL CHECK (decision IN ('approve', 'reject')),
    source          TEXT        NOT NULL CHECK (source IN ('principal', 'standing', 'expiry', 'cancel')),
    message         TEXT,
    principal       JSONB,
    standing_id     UUID        REFERENCES standing_decisions (standing_id),
    decided_at      TIMESTAMPTZ NOT NULL,
    CHECK ((source = 'principal') = (principal IS NOT NULL)),
    CHECK ((source = 'standing') = (standing_id IS NOT NULL)),
    CHECK (source NOT IN ('expiry', 'cancel') OR decision = 'reject'),
    FOREIGN KEY (interruption_id, tenant_id, project_id)
        REFERENCES interruptions (interruption_id, tenant_id, project_id)
);

ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS approval_timeout DOUBLE PRECISION;
