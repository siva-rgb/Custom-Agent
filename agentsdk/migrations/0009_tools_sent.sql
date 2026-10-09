-- FR-77 (M20): the tools a run's agent was actually sent.
--
-- tools_sent is the name and schema_hash of every tool schema the run's requests
-- carried, sorted by name: under a ContextPolicy only the tools the agent's profile may
-- execute (P2-D23), and every registered tool for a run without one. Two runs with the
-- same manifest therefore saw the same tools. tool_spec_hashes is unchanged: it still
-- lists every tool registered with the Runner.
--
-- NULL for a manifest written before this migration: a run recorded before it cannot
-- say what it was sent, and must not look as though it can.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS tools_sent JSONB;
