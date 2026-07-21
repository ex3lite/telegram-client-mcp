CREATE TABLE project_agent_settings (
    project_id UUID NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    claude_model VARCHAR(120),
    claude_effort VARCHAR(16) NOT NULL DEFAULT 'medium',
    claude_timeout_seconds INTEGER NOT NULL DEFAULT 180,
    max_budget_cents INTEGER,
    base_prompt TEXT NOT NULL DEFAULT '',
    answer_style VARCHAR(16) NOT NULL DEFAULT 'normal',
    privacy_level VARCHAR(16) NOT NULL DEFAULT 'strict',
    denied_globs JSONB NOT NULL DEFAULT '[]'::jsonb,
    telegram_group_mode VARCHAR(24) NOT NULL DEFAULT 'mentions',
    telegram_private_mode VARCHAR(24) NOT NULL DEFAULT 'all_messages',
    telegram_attach_markdown BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    updated_by_admin_id UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(updated_by_admin_id) REFERENCES admin_principals (id) ON DELETE SET NULL,
    CONSTRAINT ck_project_agent_effort
        CHECK (claude_effort IN ('low', 'medium', 'high', 'xhigh', 'max')),
    CONSTRAINT ck_project_agent_timeout
        CHECK (claude_timeout_seconds BETWEEN 10 AND 900),
    CONSTRAINT ck_project_agent_budget
        CHECK (max_budget_cents IS NULL OR max_budget_cents > 0),
    CONSTRAINT ck_project_agent_prompt_length
        CHECK (char_length(base_prompt) <= 20000),
    CONSTRAINT ck_project_agent_answer_style
        CHECK (answer_style IN ('brief', 'normal', 'detailed')),
    CONSTRAINT ck_project_agent_privacy
        CHECK (privacy_level IN ('strict', 'balanced')),
    CONSTRAINT ck_project_agent_denied_globs
        CHECK (jsonb_typeof(denied_globs) = 'array'),
    CONSTRAINT ck_project_agent_group_mode
        CHECK (telegram_group_mode IN ('commands_only', 'mentions', 'all_messages')),
    CONSTRAINT ck_project_agent_private_mode
        CHECK (telegram_private_mode IN ('commands_only', 'all_messages')),
    CONSTRAINT ck_project_agent_version CHECK (version > 0)
);

CREATE TABLE system_secrets (
    name VARCHAR(120) NOT NULL,
    ciphertext BYTEA NOT NULL,
    updated_by UUID,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (name),
    FOREIGN KEY(updated_by) REFERENCES admin_principals (id) ON DELETE SET NULL
);

ALTER TABLE service_accounts
    ADD COLUMN version INTEGER NOT NULL DEFAULT 1,
    ADD CONSTRAINT ck_service_account_version CHECK (version > 0);

ALTER TABLE interactions
    ADD COLUMN artifacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN privacy_findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT ck_interaction_artifacts CHECK (jsonb_typeof(artifacts) = 'array'),
    ADD CONSTRAINT ck_interaction_privacy_findings
        CHECK (jsonb_typeof(privacy_findings) = 'array');

CREATE TABLE agent_messages (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    service_account_id UUID NOT NULL,
    correlation_id VARCHAR(255) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    target_user_id UUID,
    target_chat_id UUID,
    text_markdown TEXT NOT NULL,
    attachment_name VARCHAR(255),
    attachment_markdown TEXT,
    privacy_findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    telegram_message_id BIGINT,
    error_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(service_account_id) REFERENCES service_accounts (id) ON DELETE RESTRICT,
    FOREIGN KEY(target_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(target_chat_id) REFERENCES telegram_chats (id) ON DELETE RESTRICT,
    CONSTRAINT uq_agent_message_idempotency
        UNIQUE (service_account_id, idempotency_key),
    CONSTRAINT ck_agent_message_target
        CHECK ((target_user_id IS NULL) <> (target_chat_id IS NULL)),
    CONSTRAINT ck_agent_message_attachment
        CHECK ((attachment_name IS NULL) = (attachment_markdown IS NULL)),
    CONSTRAINT ck_agent_message_text_length
        CHECK (char_length(text_markdown) BETWEEN 1 AND 4096),
    CONSTRAINT ck_agent_message_attachment_length
        CHECK (attachment_markdown IS NULL OR octet_length(attachment_markdown) <= 1048576),
    CONSTRAINT ck_agent_message_privacy_findings
        CHECK (jsonb_typeof(privacy_findings) = 'array'),
    CONSTRAINT ck_agent_message_status
        CHECK (status IN ('queued', 'sent', 'delivery_uncertain', 'failed'))
);

CREATE INDEX ix_agent_message_project_status ON agent_messages (project_id, status);
CREATE INDEX ix_agent_messages_correlation_id ON agent_messages (correlation_id);
