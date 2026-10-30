-- FR-138, FR-154 (M42): a command ended at a bound names the bound (DECISION-21e7f6fb).
--
-- workspace_commands gains limit_name: the bound that ended the command, 'memory_bytes' when
-- Docker reports it ran out of memory, and only ever beside killed = 'limit'. It stays NULL for
-- every other ending, and for a 'limit' whose provider cannot name the bound. Nothing else about
-- the table changes; a row is still written once.

ALTER TABLE workspace_commands ADD COLUMN IF NOT EXISTS limit_name TEXT;
ALTER TABLE workspace_commands DROP CONSTRAINT IF EXISTS workspace_commands_limit_name_check;
ALTER TABLE workspace_commands ADD CONSTRAINT workspace_commands_limit_name_check
    CHECK (limit_name IS NULL OR (killed = 'limit' AND limit_name <> ''));
