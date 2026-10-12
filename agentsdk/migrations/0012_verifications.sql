-- FR-100, FR-101 (M26): verifications, and the gate policy a run executed under.
--
-- A verification judges either a claim (subject_claim) or a plan node (subject_run,
-- subject_plan, subject_version, subject_node), never both. tenant_id and project_id are
-- NOT NULL and lead both indexes (NFR-2, NFR-25). The store checks, before it writes,
-- that the subject is in the row's own tenant, project and ledger; the foreign keys back
-- it. Rows are written once and never updated or deleted (NFR-26): no verification
-- changes the claim or node it names (P3-D15).
--
-- gate_policy is NULL for a manifest written before this migration and for a run with
-- no gate.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS verifications (
    verification_id UUID        PRIMARY KEY,
    tenant_id       TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    ledger_run_id   UUID        NOT NULL REFERENCES runs (run_id),
    subject_claim   UUID        REFERENCES claims (claim_id),
    subject_run     UUID        REFERENCES runs (run_id),
    subject_plan    UUID,
    subject_version INTEGER,
    subject_node    TEXT,
    kind            TEXT        NOT NULL CHECK (kind IN ('gate', 'critic', 'external')),
    outcome         TEXT        NOT NULL,
    detail          JSONB       NOT NULL CHECK (jsonb_typeof(detail) = 'object'),
    provenance      JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    CHECK ((kind = 'gate' AND outcome IN ('clear', 'critic_required'))
        OR (kind = 'critic' AND outcome IN ('approved', 'corrected', 'escalate'))
        OR (kind = 'external' AND outcome IN ('correct', 'incorrect'))),
    CHECK ((subject_claim IS NULL) <> (subject_run IS NULL)),
    CHECK ((subject_run IS NULL AND subject_plan IS NULL AND subject_version IS NULL AND subject_node IS NULL)
        OR (subject_run IS NOT NULL AND subject_plan IS NOT NULL AND subject_version IS NOT NULL
            AND subject_node IS NOT NULL)),
    CHECK (kind <> 'gate' OR subject_run IS NOT NULL),
    FOREIGN KEY (tenant_id, project_id, subject_plan, subject_version, subject_node)
        REFERENCES plan_node_states (tenant_id, project_id, plan_id, version, node_id)
);

CREATE INDEX IF NOT EXISTS verifications_ledger
    ON verifications (tenant_id, project_id, ledger_run_id, created_at);

CREATE INDEX IF NOT EXISTS verifications_since
    ON verifications (tenant_id, project_id, created_at);

ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS gate_policy JSONB;
