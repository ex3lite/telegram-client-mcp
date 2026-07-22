ALTER TABLE telegram_chats
    ADD CONSTRAINT uq_telegram_chat_id_project UNIQUE (id, project_id);

CREATE TABLE conversation_threads (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    chat_id UUID,
    user_id UUID,
    last_message_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_conversation_thread_id_project UNIQUE (id, project_id),
    CONSTRAINT uq_conversation_thread_scope
        UNIQUE NULLS NOT DISTINCT (project_id, chat_id, user_id),
    CONSTRAINT fk_conversation_thread_project
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_conversation_thread_chat_project
        FOREIGN KEY(chat_id, project_id)
        REFERENCES telegram_chats (id, project_id) ON DELETE CASCADE,
    CONSTRAINT fk_conversation_thread_member
        FOREIGN KEY(project_id, user_id)
        REFERENCES project_memberships (project_id, user_id) ON DELETE CASCADE,
    CONSTRAINT ck_conversation_thread_target
        CHECK (chat_id IS NOT NULL OR user_id IS NOT NULL)
);

CREATE INDEX ix_conversation_thread_project_recent
    ON conversation_threads (project_id, last_message_at);

CREATE TABLE conversation_messages (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    thread_id UUID NOT NULL,
    role VARCHAR(16) NOT NULL,
    source VARCHAR(32) NOT NULL,
    external_id VARCHAR(255),
    author_user_id UUID,
    content TEXT NOT NULL,
    privacy_findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_conversation_message_source UNIQUE (thread_id, source, external_id),
    CONSTRAINT fk_conversation_message_thread_project
        FOREIGN KEY(thread_id, project_id)
        REFERENCES conversation_threads (id, project_id) ON DELETE CASCADE,
    CONSTRAINT fk_conversation_message_author
        FOREIGN KEY(author_user_id) REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT ck_conversation_message_role
        CHECK (role IN ('user', 'assistant', 'agent', 'tool')),
    CONSTRAINT ck_conversation_message_source
        CHECK (source ~ '^[a-z][a-z0-9_.-]{1,31}$'),
    CONSTRAINT ck_conversation_message_content
        CHECK (char_length(content) BETWEEN 1 AND 32000),
    CONSTRAINT ck_conversation_message_privacy
        CHECK (jsonb_typeof(privacy_findings) = 'array')
);

CREATE INDEX ix_conversation_message_thread_recent
    ON conversation_messages (project_id, thread_id, created_at);

CREATE TABLE conversation_memories (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    thread_id UUID NOT NULL,
    kind VARCHAR(16) NOT NULL,
    memory_key VARCHAR(128) NOT NULL,
    content TEXT NOT NULL,
    privacy_findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT uq_conversation_memory_key UNIQUE (thread_id, kind, memory_key),
    CONSTRAINT fk_conversation_memory_thread_project
        FOREIGN KEY(thread_id, project_id)
        REFERENCES conversation_threads (id, project_id) ON DELETE CASCADE,
    CONSTRAINT ck_conversation_memory_kind CHECK (kind IN ('summary', 'fact')),
    CONSTRAINT ck_conversation_memory_key
        CHECK (memory_key ~ '^[a-z0-9][a-z0-9_.:-]{0,127}$'),
    CONSTRAINT ck_conversation_memory_content
        CHECK (char_length(content) BETWEEN 1 AND 32000),
    CONSTRAINT ck_conversation_memory_privacy
        CHECK (jsonb_typeof(privacy_findings) = 'array')
);

CREATE INDEX ix_conversation_memory_thread_kind
    ON conversation_memories (project_id, thread_id, kind);

ALTER TABLE project_agent_settings
    ADD COLUMN memory_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN memory_recent_messages INTEGER NOT NULL DEFAULT 24,
    ADD COLUMN memory_max_context_chars INTEGER NOT NULL DEFAULT 24000,
    ADD COLUMN telegram_streaming_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD CONSTRAINT ck_project_agent_memory_recent
        CHECK (memory_recent_messages BETWEEN 4 AND 100),
    ADD CONSTRAINT ck_project_agent_memory_context
        CHECK (memory_max_context_chars BETWEEN 3000 AND 100000);

ALTER TABLE interactions
    ADD COLUMN conversation_thread_id UUID,
    ADD CONSTRAINT fk_interaction_conversation_project
        FOREIGN KEY(conversation_thread_id, project_id)
        REFERENCES conversation_threads (id, project_id)
        ON DELETE SET NULL (conversation_thread_id);
