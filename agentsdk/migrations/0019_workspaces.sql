-- FR-140 (M38): workspaces and the commands they ran (P5-D13).
--
-- workspaces holds one row per workspace a Runner created: its provider, the image's digest, the
-- limits in force (WorkspaceSpec.record(), env names and never values), the run it serves and its
-- scope. Its removal columns are its only update, made once: an UPDATE may only fill them while
-- both are NULL, and nothing else about a row ever changes.
--
-- workspace_commands holds one row per command, written once when it ends: its argv, how it
-- ended, when, and the bytes each stream produced and kept. The output itself lives only in the
-- tool result. Every row carries its tenant and project (NFR-2).
--
-- execution_manifests gains `workspace`: the provider and the spec's record, NULL for a run
-- without a workspace (NFR-41).
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id   UUID        PRIMARY KEY,
    tenant_id      TEXT        NOT NULL,
    project_id     TEXT        NOT NULL,
    run_id         UUID        NOT NULL REFERENCES runs (run_id),
    scope          TEXT        NOT NULL CHECK (scope IN ('run', 'plan')),
    provider       TEXT        NOT NULL,
    image          TEXT        NOT NULL CHECK (image ~ '@sha256:[0-9a-f]{64}$'),
    limits         JSONB       NOT NULL CHECK (jsonb_typeof(limits) = 'object'),
    created_at     TIMESTAMPTZ NOT NULL,
    removed_at     TIMESTAMPTZ,
    removed_reason TEXT        CHECK (removed_reason IN ('run_ended', 'released', 'aclose', 'reaped')),
    CHECK ((removed_at IS NULL) = (removed_reason IS NULL))
);

CREATE INDEX IF NOT EXISTS workspaces_run ON workspaces (tenant_id, project_id, run_id);

CREATE TABLE IF NOT EXISTS workspace_commands (
    command_id   UUID        PRIMARY KEY,
    workspace_id UUID        NOT NULL REFERENCES workspaces (workspace_id),
    tenant_id    TEXT        NOT NULL,
    project_id   TEXT        NOT NULL,
    run_id       UUID        NOT NULL REFERENCES runs (run_id),
    tool_call_id TEXT,
    argv         JSONB       NOT NULL CHECK (jsonb_typeof(argv) = 'array'),
    exit_code    INTEGER,
    killed       TEXT        CHECK (killed IN ('timeout', 'cancelled', 'limit')),
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ NOT NULL,
    stdout_bytes BIGINT      NOT NULL CHECK (stdout_bytes >= 0),
    stderr_bytes BIGINT      NOT NULL CHECK (stderr_bytes >= 0),
    stdout_kept  BIGINT      NOT NULL CHECK (stdout_kept >= 0 AND stdout_kept <= stdout_bytes),
    stderr_kept  BIGINT      NOT NULL CHECK (stderr_kept >= 0 AND stderr_kept <= stderr_bytes),
    CHECK (ended_at >= started_at),
    CHECK ((exit_code IS NULL) = (killed IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS workspace_commands_run ON workspace_commands (tenant_id, project_id, run_id, started_at);

-- Written once, removal once: a row's fields never change, and its removal is filled only from NULL.
CREATE OR REPLACE FUNCTION workspaces_removed_once() RETURNS trigger AS $$
BEGIN
    IF OLD.removed_at IS NOT NULL
       OR (NEW.workspace_id, NEW.tenant_id, NEW.project_id, NEW.run_id, NEW.scope, NEW.provider, NEW.image,
           NEW.limits, NEW.created_at)
          IS DISTINCT FROM
          (OLD.workspace_id, OLD.tenant_id, OLD.project_id, OLD.run_id, OLD.scope, OLD.provider, OLD.image,
           OLD.limits, OLD.created_at) THEN
        RAISE EXCEPTION 'a workspace row is written once, and its removal once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workspaces_removed_once ON workspaces;
CREATE TRIGGER workspaces_removed_once BEFORE UPDATE ON workspaces
    FOR EACH ROW EXECUTE FUNCTION workspaces_removed_once();

CREATE OR REPLACE FUNCTION workspace_commands_written_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'a workspace command is written once';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workspace_commands_written_once ON workspace_commands;
CREATE TRIGGER workspace_commands_written_once BEFORE UPDATE ON workspace_commands
    FOR EACH ROW EXECUTE FUNCTION workspace_commands_written_once();

ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS workspace JSONB CHECK (workspace IS NULL OR jsonb_typeof(workspace) = 'object');
