-- FR-162 (M44): every connection through a workspace's egress proxy (P5-D23 as amended by
-- DECISION-91ec4213; DECISION-88dea779).
--
-- One row per connection the proxy decided, allowed or refused, written from the proxy's own report,
-- never a command's. command_id is NULL when the connection named no command. A tunnel's bytes each
-- way are filled when it ends; a 'connection_limit' row stands for every connection refused at the
-- proxy's limit, and an 'unrecorded' row for every report the manager could not record within its
-- workspace's removal bound, and count says how many (DECISION-e510a5e3). Nothing a command sends to
-- identify itself is stored.

CREATE TABLE IF NOT EXISTS workspace_connections (
    connection_id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces (workspace_id),
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    run_id UUID NOT NULL,
    command_id UUID,
    host TEXT,
    port INTEGER CHECK (port IS NULL OR port BETWEEN 1 AND 65535),
    address TEXT,
    allowed BOOLEAN NOT NULL,
    reason TEXT NOT NULL CHECK (reason <> ''),
    at TIMESTAMPTZ NOT NULL,
    bytes_out BIGINT CHECK (bytes_out IS NULL OR bytes_out >= 0),
    bytes_in BIGINT CHECK (bytes_in IS NULL OR bytes_in >= 0),
    count INTEGER CHECK (count IS NULL OR count > 0),
    CHECK (allowed IS NOT TRUE OR reason = 'allowed'),
    CHECK ((reason IN ('connection_limit', 'unrecorded')) = (count IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS workspace_connections_tenancy ON workspace_connections (tenant_id, project_id, run_id);
