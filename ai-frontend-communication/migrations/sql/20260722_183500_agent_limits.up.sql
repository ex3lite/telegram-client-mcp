ALTER TABLE project_agent_settings
    DROP CONSTRAINT ck_project_agent_timeout,
    DROP CONSTRAINT ck_project_agent_memory_recent,
    DROP CONSTRAINT ck_project_agent_memory_context;

ALTER TABLE project_agent_settings
    ALTER COLUMN claude_timeout_seconds SET DEFAULT 1200,
    ALTER COLUMN memory_recent_messages SET DEFAULT 200,
    ALTER COLUMN memory_max_context_chars SET DEFAULT 500000,
    ADD CONSTRAINT ck_project_agent_timeout
        CHECK (claude_timeout_seconds BETWEEN 10 AND 3600),
    ADD CONSTRAINT ck_project_agent_memory_recent
        CHECK (memory_recent_messages BETWEEN 4 AND 500),
    ADD CONSTRAINT ck_project_agent_memory_context
        CHECK (memory_max_context_chars BETWEEN 3000 AND 1000000);
