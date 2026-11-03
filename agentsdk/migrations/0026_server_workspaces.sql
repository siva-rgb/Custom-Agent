-- FR-165 (M45): a sandboxed MCP server's workspace (P5-D21, P5-D23 as amended).
--
-- A server's connection is shared per alias, tenant and project and may open with no run, so its
-- workspace belongs to its Runner's tenancy: recorded with scope 'server' and no run, and only a
-- server's workspace has no run (DECISION-2a12196e). Its connections through its egress are rows
-- with no run either, and only a server workspace's (DECISION-61aed587).
--
-- Idempotent (FR-17): each constraint is dropped by name first. Nothing already stored changes:
-- every existing row has a run and scope 'run' or 'plan'.

ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspaces_scope_check;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_scope_check CHECK (scope IN ('run', 'plan', 'server'));
ALTER TABLE workspaces ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspaces_run_by_scope;
ALTER TABLE workspaces ADD CONSTRAINT workspaces_run_by_scope CHECK ((run_id IS NULL) = (scope = 'server'));

ALTER TABLE workspace_connections ALTER COLUMN run_id DROP NOT NULL;

CREATE OR REPLACE FUNCTION workspace_connections_run_by_scope() RETURNS trigger AS $$
BEGIN
    IF (NEW.run_id IS NULL) IS DISTINCT FROM
       ((SELECT scope FROM workspaces WHERE workspace_id = NEW.workspace_id) = 'server') THEN
        RAISE EXCEPTION 'a connection has no run exactly when its workspace is a server''s';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workspace_connections_run_by_scope ON workspace_connections;
CREATE TRIGGER workspace_connections_run_by_scope BEFORE INSERT ON workspace_connections
    FOR EACH ROW EXECUTE FUNCTION workspace_connections_run_by_scope();
