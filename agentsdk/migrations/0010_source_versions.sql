-- FR-89 (M22): source versions, one immutable row per fetch (or copy) of a source.
--
-- tenant_id and project_id are NOT NULL and lead the scope index (NFR-2, NFR-25). The
-- content is an M13 artifact, named by artifact_id and verified against content_hash on
-- every read; artifact_id is not a foreign key, because an artifact that can no longer be
-- read makes its version a cache miss rather than an error (FR-90). source_run references
-- runs, so test cleanup removes versions before runs. ledger_run_id is the top-level run a
-- SESSION version is served within; request_variant is part of the cache key and None for
-- the built-in fetch. Nothing updates or deletes a row (NFR-26): a refresh is a new row
-- naming the one it replaced in prior_version.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS source_versions (
    source_version_id UUID        PRIMARY KEY,
    tenant_id         TEXT        NOT NULL,
    project_id        TEXT        NOT NULL,
    canonical_uri     TEXT        NOT NULL,
    final_uri         TEXT        NOT NULL,
    auth_scope_hash   TEXT,
    request_variant   TEXT,
    retrieval_time    TIMESTAMPTZ NOT NULL,
    content_hash      TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    artifact_id       UUID        NOT NULL,
    media_type        TEXT        NOT NULL,
    cache_scope       TEXT        NOT NULL
        CHECK (cache_scope IN ('public_global', 'tenant', 'project', 'session', 'no_cache')),
    prior_version     UUID        REFERENCES source_versions (source_version_id),
    provenance        JSONB       NOT NULL,
    source_run        UUID        NOT NULL REFERENCES runs (run_id),
    ledger_run_id     UUID        NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- ADR-31: authenticated content is never globally shared.
    CONSTRAINT source_versions_public_unauthenticated
        CHECK (cache_scope <> 'public_global' OR auth_scope_hash IS NULL)
);

CREATE INDEX IF NOT EXISTS source_versions_tenant_project
    ON source_versions (tenant_id, project_id, canonical_uri);

-- The cache lookup: a key, across the scopes that may reach the reader.
CREATE INDEX IF NOT EXISTS source_versions_key
    ON source_versions (canonical_uri, retrieval_time DESC);
