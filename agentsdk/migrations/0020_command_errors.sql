-- FR-148 (M41): a command whose provider failed is recorded too (M40 caveat 2, DECISION-825b81a6).
--
-- workspace_commands.killed admits 'error': the provider raised with the command begun, so it has
-- no exit code, and its WorkspaceCommandEnded names the exception's class as kill_error. Every
-- command that started then has one row. Nothing else about the table changes.

ALTER TABLE workspace_commands DROP CONSTRAINT IF EXISTS workspace_commands_killed_check;
ALTER TABLE workspace_commands ADD CONSTRAINT workspace_commands_killed_check
    CHECK (killed IN ('timeout', 'cancelled', 'limit', 'error'));
