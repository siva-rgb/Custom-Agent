-- FR-138 (M42): 0021's check let a bound stand beside a command not killed at a limit.
--
-- `killed = 'limit'` is NULL, not false, when killed is NULL, and a CHECK that is NULL passes: a
-- row with limit_name set and killed NULL was admitted. The check is replaced by one that is
-- false there. 0021 is applied, so it is corrected here rather than edited.

ALTER TABLE workspace_commands DROP CONSTRAINT IF EXISTS workspace_commands_limit_name_check;
ALTER TABLE workspace_commands ADD CONSTRAINT workspace_commands_limit_name_check
    CHECK (limit_name IS NULL OR (killed IS NOT DISTINCT FROM 'limit' AND limit_name <> ''));
