ALTER TABLE agent_decisions
ADD COLUMN timeout_count INTEGER NOT NULL DEFAULT 0 CHECK (timeout_count >= 0);
