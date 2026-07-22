ALTER TABLE interactions
    DROP CONSTRAINT fk_interaction_conversation_project,
    DROP COLUMN conversation_thread_id;

ALTER TABLE project_agent_settings
    DROP CONSTRAINT ck_project_agent_memory_context,
    DROP CONSTRAINT ck_project_agent_memory_recent,
    DROP COLUMN memory_max_context_chars,
    DROP COLUMN memory_recent_messages,
    DROP COLUMN memory_enabled,
    DROP COLUMN telegram_streaming_enabled;

DROP TABLE conversation_memories;
DROP TABLE conversation_messages;
DROP TABLE conversation_threads;

ALTER TABLE telegram_chats
    DROP CONSTRAINT uq_telegram_chat_id_project;
