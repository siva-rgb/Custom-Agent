-- FR-176, FR-177 (M49): the manifest records the preset a run's spec came from, as its name and
-- version, and the role_turns an orchestrator's run briefed its children with (P5A-D13,
-- DECISION-05d0a716). Each is NULL for a run without one, and for every manifest written before
-- M49 (NFR-50).
--
-- Idempotent (FR-17). schema.sql is NOT edited.
ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS preset JSONB CHECK (preset IS NULL OR jsonb_typeof(preset) = 'object'),
    ADD COLUMN IF NOT EXISTS role_turns JSONB CHECK (role_turns IS NULL OR jsonb_typeof(role_turns) = 'object');
