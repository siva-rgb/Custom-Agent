-- FR-127, FR-128 (M34): the MCP catalog, and standing decisions bound to a tool hash.
--
-- mcp_tool_versions holds one row per version of a tool (or, from M35, a prompt or resource
-- template) that a tenancy's connection has seen, unique on its tenant, project, alias and
-- hash; mcp_tool_acceptances one row per accepted hash, referencing its version by that same
-- composite key, so no tenancy can accept a version it has not seen. Rows are written once and
-- never updated or deleted (NFR-2, NFR-36).
--
-- standing_decisions gains a nullable tool_hash: a standing decision on an MCP tool applies
-- only to a request with the same hash, so a call after an accepted change is asked again.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS mcp_tool_versions (
    tenant_id     TEXT        NOT NULL,
    project_id    TEXT        NOT NULL,
    kind          TEXT        NOT NULL CHECK (kind IN ('tool', 'prompt', 'resource_template')),
    alias         TEXT        NOT NULL,
    tool_name     TEXT        NOT NULL,
    tool_hash     TEXT        NOT NULL CHECK (tool_hash ~ '^[0-9a-f]{64}$'),
    entry         JSONB       NOT NULL CHECK (jsonb_typeof(entry) = 'object'),
    first_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, project_id, alias, tool_hash)
);

CREATE TABLE IF NOT EXISTS mcp_tool_acceptances (
    acceptance_id UUID        PRIMARY KEY,
    seq           BIGSERIAL   NOT NULL UNIQUE,
    tenant_id     TEXT        NOT NULL,
    project_id    TEXT        NOT NULL,
    alias         TEXT        NOT NULL,
    tool_name     TEXT        NOT NULL,
    tool_hash     TEXT        NOT NULL,
    principal     JSONB       NOT NULL,
    accepted_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, project_id, alias, tool_hash),
    FOREIGN KEY (tenant_id, project_id, alias, tool_hash)
        REFERENCES mcp_tool_versions (tenant_id, project_id, alias, tool_hash)
);

CREATE INDEX IF NOT EXISTS mcp_tool_acceptances_tool
    ON mcp_tool_acceptances (tenant_id, project_id, alias, tool_name, seq);

ALTER TABLE standing_decisions
    ADD COLUMN IF NOT EXISTS tool_hash TEXT CHECK (tool_hash IS NULL OR tool_hash ~ '^[0-9a-f]{64}$');
