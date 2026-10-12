-- FR-110 (M26a): the schema refuses a verification of another tenant's, project's or
-- ledger's claim, as migration 0012's comment said it did.
--
-- 0012 backed node subjects with a foreign key to plan_node_states, tenant and project
-- included, but a claim subject only referenced claims (claim_id): the store's
-- transactional check was the one thing keeping a verification of one ledger from naming
-- another ledger's claim (M26 review). Here claims gain a unique key over the claim and
-- its tenant, project and ledger, and a verification's claim subject references it with
-- the row's own tenant, project and ledger. MATCH SIMPLE: a node subject, whose
-- subject_claim is NULL, is not checked by it. 0012 is not edited: its checksum is
-- recorded wherever it was applied.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_ledger_identity'
                   AND conrelid = 'claims'::regclass) THEN
        ALTER TABLE claims
            ADD CONSTRAINT claims_ledger_identity UNIQUE (claim_id, tenant_id, project_id, ledger_run_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'verifications_claim_in_ledger'
                   AND conrelid = 'verifications'::regclass) THEN
        ALTER TABLE verifications
            ADD CONSTRAINT verifications_claim_in_ledger
            FOREIGN KEY (subject_claim, tenant_id, project_id, ledger_run_id)
            REFERENCES claims (claim_id, tenant_id, project_id, ledger_run_id);
    END IF;
END
$$;
