-- M30 pre-flight (M29 review finding 1, KNOWLEDGE-7ebaabe3): a decision references the
-- standing decision it applied with its own tenant and project, as every other reference
-- 0014 made does.
--
-- 0014 gave interruption_decisions.standing_id a single-column reference, so the schema
-- alone let a decision name another tenant's standing decision -- the asymmetry 0013 fixed
-- for claims one migration earlier. Here standing_decisions gains a unique key over the id
-- and its tenancy, and the reference becomes composite over it. 0014 is not edited: its
-- checksum is recorded wherever it was applied.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
DO $$
DECLARE
    single TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'standing_decisions_tenancy_identity'
                   AND conrelid = 'standing_decisions'::regclass) THEN
        ALTER TABLE standing_decisions
            ADD CONSTRAINT standing_decisions_tenancy_identity UNIQUE (standing_id, tenant_id, project_id);
    END IF;
    FOR single IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'interruption_decisions'::regclass AND contype = 'f'
          AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (standing_id) %'
    LOOP
        EXECUTE format('ALTER TABLE interruption_decisions DROP CONSTRAINT %I', single);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruption_decisions_standing_in_tenancy'
                   AND conrelid = 'interruption_decisions'::regclass) THEN
        ALTER TABLE interruption_decisions
            ADD CONSTRAINT interruption_decisions_standing_in_tenancy
            FOREIGN KEY (standing_id, tenant_id, project_id)
            REFERENCES standing_decisions (standing_id, tenant_id, project_id);
    END IF;
END
$$;
