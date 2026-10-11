-- FR-92 (M23): the evidence ledger -- claims, their contributions, and what each cites.
--
-- tenant_id and project_id are NOT NULL and lead the ledger index (NFR-2, NFR-25). A
-- claim belongs to the ledger of its top-level run (ledger_run_id, P3-D2). Its sources
-- and labels are not stored on it: they are derived from its contributions when it is
-- read, so a claim row and every contribution are written once and never updated or
-- deleted (NFR-26). A supersession is its own claim naming the one it replaces; UNIQUE
-- (supersedes) makes the history a chain that cannot fork even if two writers race past
-- the store's lock. A citation references the source version it names; the store checks
-- that the version is in the claim's own tenant and project before it writes.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS claims (
    claim_id      UUID        PRIMARY KEY,
    tenant_id     TEXT        NOT NULL,
    project_id    TEXT        NOT NULL,
    ledger_run_id UUID        NOT NULL REFERENCES runs (run_id),
    statement     TEXT        NOT NULL,
    claim_key     TEXT        NOT NULL CHECK (claim_key ~ '^[0-9a-f]{64}$'),
    supersedes    UUID        UNIQUE REFERENCES claims (claim_id),
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS claims_ledger_key
    ON claims (tenant_id, project_id, ledger_run_id, claim_key);

CREATE TABLE IF NOT EXISTS claim_contributions (
    contribution_id UUID             PRIMARY KEY,
    claim_id        UUID             NOT NULL REFERENCES claims (claim_id),
    tenant_id       TEXT             NOT NULL,
    project_id      TEXT             NOT NULL,
    run_id          UUID             NOT NULL REFERENCES runs (run_id),
    node_id         TEXT,
    agent_id        TEXT             NOT NULL,
    confidence      DOUBLE PRECISION CHECK (confidence >= 0 AND confidence <= 1),
    provenance      JSONB            NOT NULL,
    why             TEXT,
    recorded_at     TIMESTAMPTZ      NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS claim_contributions_claim
    ON claim_contributions (tenant_id, project_id, claim_id, recorded_at);

CREATE TABLE IF NOT EXISTS claim_citations (
    contribution_id   UUID    NOT NULL REFERENCES claim_contributions (contribution_id),
    stance            TEXT    NOT NULL CHECK (stance IN ('supporting', 'contradicting')),
    position          INTEGER NOT NULL CHECK (position >= 0),
    source_version_id UUID    NOT NULL REFERENCES source_versions (source_version_id),
    PRIMARY KEY (contribution_id, stance, position)
);
