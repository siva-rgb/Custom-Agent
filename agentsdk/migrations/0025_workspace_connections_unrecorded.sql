-- FR-162 (M44 round 5 N6): a workspace has at most one 'unrecorded' row, its count updated as more
-- is counted (DECISION-f7114b8b). The manager kept that rule in memory alone; the store now holds
-- it. A second migration in M44 because 0024 was already applied when its review found the index
-- missing, and an applied migration is never edited (DECISION-7a3a2ae2).

CREATE UNIQUE INDEX IF NOT EXISTS workspace_connections_one_unrecorded
    ON workspace_connections (workspace_id) WHERE reason = 'unrecorded';
