-- FR-67, FR-68, FR-69 (M17): what a run was allowed to spend, and what it spent.
--
-- budget_policy is the effective BudgetPolicy, budget_reservations the reservation
-- this run held (keyed by plan node), and budget_spend its final spend, written when
-- the run ends because it is not known when the manifest row is created. Money is
-- stored as text inside the JSONB, never as a JSON number: a float would drift.
--
-- price_table_date is the date of the shipped price table when that table is what
-- priced the run, so a stale price can never be read as current fact (FR-69).
--
-- All four are NULL for a manifest written before this migration, and for any run
-- that carried no budget: a run that had none must not look as though it had one.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS budget_policy       JSONB,
    ADD COLUMN IF NOT EXISTS budget_reservations JSONB,
    ADD COLUMN IF NOT EXISTS budget_spend        JSONB,
    ADD COLUMN IF NOT EXISTS price_table_date    TEXT;
