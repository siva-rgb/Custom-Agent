-- FR-65, FR-66 (M16): a run's plan versions, and each node's status.
--
-- tenant_id and project_id are NOT NULL and lead every key and index (NFR-2, ADR-11):
-- a plan is identified by tenant, project, plan_id and version, so one tenant can
-- neither take another's plan id nor learn that it exists (DECISION-6d073ac0, F1).
-- Both tables reference runs, with no ON DELETE, so removing a run that a plan still
-- names is refused and test cleanup removes node states and plans first.
--
-- A version is never updated: a replan inserts a new row whose parent names the
-- version it replaced, and that reference is a foreign key within the same scope, so
-- a parent that was never stored cannot be named. The document is the canonical JSON
-- text the plan is hashed over, stored as TEXT because JSONB rewrites numbers (1e16
-- reads back as 10000000000000000, -0.0 as 0) and the plan would read back with
-- another hash (F3). The store compares the hash on every read.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS plan_versions (
    tenant_id      TEXT        NOT NULL,
    project_id     TEXT        NOT NULL,
    plan_id        UUID        NOT NULL,
    version        INTEGER     NOT NULL CHECK (version >= 1),
    run_id         UUID        NOT NULL REFERENCES runs (run_id),
    parent_plan_id UUID,
    parent_version INTEGER,
    plan_hash      TEXT        NOT NULL,
    document       TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, project_id, plan_id, version),
    FOREIGN KEY (tenant_id, project_id, parent_plan_id, parent_version)
        REFERENCES plan_versions (tenant_id, project_id, plan_id, version),
    CHECK ((parent_plan_id IS NULL) = (parent_version IS NULL))
);

CREATE INDEX IF NOT EXISTS plan_versions_run ON plan_versions (run_id);

CREATE TABLE IF NOT EXISTS plan_node_states (
    tenant_id  TEXT        NOT NULL,
    project_id TEXT        NOT NULL,
    plan_id    UUID        NOT NULL,
    version    INTEGER     NOT NULL,
    node_id    TEXT        NOT NULL,
    run_id     UUID        NOT NULL REFERENCES runs (run_id),
    status     TEXT        NOT NULL
               CHECK (status IN ('pending', 'ready', 'running', 'done', 'failed', 'skipped', 'cancelled')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, plan_id, version, node_id),
    FOREIGN KEY (tenant_id, project_id, plan_id, version)
        REFERENCES plan_versions (tenant_id, project_id, plan_id, version)
);

CREATE INDEX IF NOT EXISTS plan_node_states_run ON plan_node_states (run_id);
