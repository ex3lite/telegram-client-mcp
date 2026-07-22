UPDATE project_agent_settings
SET claude_timeout_seconds = LEAST(claude_timeout_seconds, 900),
    memory_recent_messages = LEAST(memory_recent_messages, 100),
    memory_max_context_chars = LEAST(memory_max_context_chars, 100000);

ALTER TABLE project_agent_settings
    DROP CONSTRAINT ck_project_agent_timeout,
    DROP CONSTRAINT ck_project_agent_memory_recent,
    DROP CONSTRAINT ck_project_agent_memory_context;

ALTER TABLE project_agent_settings
    ALTER COLUMN claude_timeout_seconds SET DEFAULT 180,
    ALTER COLUMN memory_recent_messages SET DEFAULT 24,
    ALTER COLUMN memory_max_context_chars SET DEFAULT 24000,
    ADD CONSTRAINT ck_project_agent_timeout
        CHECK (claude_timeout_seconds BETWEEN 10 AND 900),
    ADD CONSTRAINT ck_project_agent_memory_recent
        CHECK (memory_recent_messages BETWEEN 4 AND 100),
    ADD CONSTRAINT ck_project_agent_memory_context
        CHECK (memory_max_context_chars BETWEEN 3000 AND 100000);
