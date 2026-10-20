-- FR-123 (M33): the manifest records each MCP server a run's Runner was given -- its alias,
-- its command and arguments or its URL, and the names, never the values, of its environment
-- variables and headers (NFR-37). An empty list for a Runner with none; NULL for every
-- manifest written before M33.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS mcp_servers JSONB CHECK (mcp_servers IS NULL OR jsonb_typeof(mcp_servers) = 'array');
