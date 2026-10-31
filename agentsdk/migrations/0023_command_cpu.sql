-- FR-156 (M43): every command's CPU is measured (P5-D17, P5-D23 as amended by DECISION-91ec4213).
--
-- workspace_commands gains cpu_usec: the CPU-microseconds the command is charged, its own reading
-- or else its wall time times its cpus limit. It is NULL only for rows written before M43. M44's
-- and M46's tables come with their own migrations, 0024 and 0025. Nothing else changes.

ALTER TABLE workspace_commands ADD COLUMN IF NOT EXISTS cpu_usec BIGINT;
ALTER TABLE workspace_commands DROP CONSTRAINT IF EXISTS workspace_commands_cpu_usec_check;
ALTER TABLE workspace_commands ADD CONSTRAINT workspace_commands_cpu_usec_check
    CHECK (cpu_usec IS NULL OR cpu_usec >= 0);
