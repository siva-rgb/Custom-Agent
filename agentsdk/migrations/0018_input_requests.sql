-- FR-133 (M36): an MCP server's request for more input is an interruption too.
--
-- interruptions admits two more kinds, additional_user_input and external_tool_input, whose
-- trigger is server, and only theirs. interruption_decisions admits their answers -- accept,
-- decline and cancel -- with approve and reject kept for approvals; an input request's expiry
-- and its run's cancellation decide cancel, an approval's still reject; a request the client does
-- not support (sampling, roots) is declined with source unsupported. A nullable response holds an
-- accepted form's content.
--
-- Which answers suit an interruption depends on its kind, in another table, so a trigger checks
-- every decision against its interruption's kind and refuses an input decision of approve or an
-- approval decision of accept as a check violation. Rows stay insert-only (0014). 0014 and 0015
-- are applied and not edited; their constraints are replaced here by name.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
ALTER TABLE interruptions DROP CONSTRAINT IF EXISTS interruptions_kind_check;
ALTER TABLE interruptions DROP CONSTRAINT IF EXISTS interruptions_trigger_check;
ALTER TABLE interruption_decisions DROP CONSTRAINT IF EXISTS interruption_decisions_decision_check;
ALTER TABLE interruption_decisions DROP CONSTRAINT IF EXISTS interruption_decisions_source_check;
ALTER TABLE interruption_decisions DROP CONSTRAINT IF EXISTS interruption_decisions_check2;

ALTER TABLE interruption_decisions ADD COLUMN IF NOT EXISTS response JSONB;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruptions_kind_known'
                   AND conrelid = 'interruptions'::regclass) THEN
        ALTER TABLE interruptions ADD CONSTRAINT interruptions_kind_known
            CHECK (kind IN ('tool_approval', 'additional_user_input', 'external_tool_input'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruptions_trigger_known'
                   AND conrelid = 'interruptions'::regclass) THEN
        ALTER TABLE interruptions ADD CONSTRAINT interruptions_trigger_known
            CHECK (trigger IN ('policy', 'predicate', 'hook', 'server'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruptions_server_asks_input'
                   AND conrelid = 'interruptions'::regclass) THEN
        ALTER TABLE interruptions ADD CONSTRAINT interruptions_server_asks_input
            CHECK ((trigger = 'server') = (kind <> 'tool_approval'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruption_decisions_decision_known'
                   AND conrelid = 'interruption_decisions'::regclass) THEN
        ALTER TABLE interruption_decisions ADD CONSTRAINT interruption_decisions_decision_known
            CHECK (decision IN ('approve', 'reject', 'accept', 'decline', 'cancel'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruption_decisions_source_known'
                   AND conrelid = 'interruption_decisions'::regclass) THEN
        ALTER TABLE interruption_decisions ADD CONSTRAINT interruption_decisions_source_known
            CHECK (source IN ('principal', 'standing', 'expiry', 'cancel', 'unsupported'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruption_decisions_ended_by'
                   AND conrelid = 'interruption_decisions'::regclass) THEN
        ALTER TABLE interruption_decisions ADD CONSTRAINT interruption_decisions_ended_by
            CHECK (source NOT IN ('expiry', 'cancel') OR decision IN ('reject', 'cancel'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruption_decisions_unsupported_declined'
                   AND conrelid = 'interruption_decisions'::regclass) THEN
        ALTER TABLE interruption_decisions ADD CONSTRAINT interruption_decisions_unsupported_declined
            CHECK (source <> 'unsupported' OR decision = 'decline');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'interruption_decisions_response_accepted'
                   AND conrelid = 'interruption_decisions'::regclass) THEN
        ALTER TABLE interruption_decisions ADD CONSTRAINT interruption_decisions_response_accepted
            CHECK (response IS NULL OR (decision = 'accept' AND jsonb_typeof(response) = 'object'));
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION interruption_decision_suits_kind() RETURNS trigger AS $$
DECLARE
    asked TEXT;
BEGIN
    SELECT kind INTO asked FROM interruptions
    WHERE interruption_id = NEW.interruption_id AND tenant_id = NEW.tenant_id AND project_id = NEW.project_id;
    IF asked IS NULL THEN
        RETURN NEW;  -- the composite foreign key refuses it
    END IF;
    IF (asked = 'tool_approval') <> (NEW.decision IN ('approve', 'reject')) THEN
        RAISE EXCEPTION 'interruption % is %, and % is not its answer', NEW.interruption_id, asked, NEW.decision
            USING ERRCODE = 'check_violation';
    END IF;
    IF asked <> 'tool_approval' AND NEW.source IN ('expiry', 'cancel') AND NEW.decision <> 'cancel' THEN
        RAISE EXCEPTION 'an input request''s % decides cancel', NEW.source USING ERRCODE = 'check_violation';
    END IF;
    IF asked = 'tool_approval' AND NEW.source = 'unsupported' THEN
        RAISE EXCEPTION 'an approval is never unsupported' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS interruption_decisions_suit_kind ON interruption_decisions;
CREATE TRIGGER interruption_decisions_suit_kind
    BEFORE INSERT OR UPDATE ON interruption_decisions
    FOR EACH ROW EXECUTE FUNCTION interruption_decision_suits_kind();
