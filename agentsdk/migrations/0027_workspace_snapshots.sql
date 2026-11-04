-- FR-168 (M46): snapshots of workspaces (P5-D22, P5-D23).
--
-- workspace_snapshots holds one row per snapshot a Runner took: the workspace it copied, of the
-- same tenant and project, and its size as measured when taken. Its removal columns are its only
-- update, made once: released, removed with aclose() when its workspace was not kept
-- (DECISION-bafa7be8), or reaped once its owner is gone. Every row carries its tenant and project
-- (NFR-2).
--
-- Idempotent (FR-17). Nothing already stored changes: the table is new.

CREATE TABLE IF NOT EXISTS workspace_snapshots (
    snapshot_id    UUID        PRIMARY KEY,
    workspace_id   UUID        NOT NULL REFERENCES workspaces (workspace_id),
    tenant_id      TEXT        NOT NULL,
    project_id     TEXT        NOT NULL,
    size_bytes     BIGINT      NOT NULL CHECK (size_bytes >= 0),
    created_at     TIMESTAMPTZ NOT NULL,
    removed_at     TIMESTAMPTZ,
    removed_reason TEXT        CHECK (removed_reason IN ('released', 'aclose', 'reaped')),
    CHECK ((removed_at IS NULL) = (removed_reason IS NULL))
);

CREATE INDEX IF NOT EXISTS workspace_snapshots_tenancy ON workspace_snapshots (tenant_id, project_id, created_at);

-- A snapshot is of its own tenancy's workspace; written once, and its removal filled only from NULL.
CREATE OR REPLACE FUNCTION workspace_snapshots_checked() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (SELECT 1 FROM workspaces WHERE workspace_id = NEW.workspace_id
                       AND tenant_id = NEW.tenant_id AND project_id = NEW.project_id) THEN
            RAISE EXCEPTION 'a snapshot is of a workspace of its own tenant and project';
        END IF;
    ELSIF OLD.removed_at IS NOT NULL
       OR (NEW.snapshot_id, NEW.workspace_id, NEW.tenant_id, NEW.project_id, NEW.size_bytes, NEW.created_at)
          IS DISTINCT FROM
          (OLD.snapshot_id, OLD.workspace_id, OLD.tenant_id, OLD.project_id, OLD.size_bytes, OLD.created_at) THEN
        RAISE EXCEPTION 'a snapshot row is written once, and its removal once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workspace_snapshots_checked ON workspace_snapshots;
CREATE TRIGGER workspace_snapshots_checked BEFORE INSERT OR UPDATE ON workspace_snapshots
    FOR EACH ROW EXECUTE FUNCTION workspace_snapshots_checked();
