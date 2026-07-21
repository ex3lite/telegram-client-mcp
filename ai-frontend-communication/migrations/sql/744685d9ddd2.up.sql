CREATE TABLE jobs (
    id UUID NOT NULL,
    kind VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL,
    deduplication_key VARCHAR(255),
    attempts INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    locked_at TIMESTAMP WITH TIME ZONE,
    locked_by VARCHAR(255),
    result JSONB,
    last_error_code VARCHAR(64),
    last_error_detail TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_job_deduplication_key UNIQUE (deduplication_key)
);

CREATE INDEX ix_job_claim ON jobs (status, available_at, created_at);

CREATE TABLE projects (
    id UUID NOT NULL,
    slug VARCHAR(80) NOT NULL,
    name VARCHAR(160) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (slug)
);

CREATE TABLE service_accounts (
    id UUID NOT NULL,
    name VARCHAR(120) NOT NULL,
    token_prefix VARCHAR(8) NOT NULL,
    token_hash VARCHAR(512) NOT NULL,
    tool_scopes JSONB NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE,
    active BOOLEAN NOT NULL,
    last_used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (name),
    UNIQUE (token_prefix)
);

CREATE TABLE telegram_updates (
    update_id BIGSERIAL NOT NULL,
    update_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (update_id)
);

CREATE TABLE users (
    id UUID NOT NULL,
    display_name VARCHAR(160) NOT NULL,
    email VARCHAR(320),
    active BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (email)
);

CREATE TABLE audit_events (
    id UUID NOT NULL,
    event_type VARCHAR(120) NOT NULL,
    event_version INTEGER NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    project_id UUID,
    actor_type VARCHAR(32) NOT NULL,
    actor_id VARCHAR(255) NOT NULL,
    correlation_id VARCHAR(255) NOT NULL,
    causation_id UUID,
    subject_type VARCHAR(64),
    subject_id VARCHAR(255),
    outcome VARCHAR(32) NOT NULL,
    payload JSONB NOT NULL,
    remote_address_hash BYTEA,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE SET NULL
);

CREATE INDEX ix_audit_correlation_time ON audit_events (correlation_id, occurred_at);
CREATE INDEX ix_audit_project_time ON audit_events (project_id, occurred_at);

CREATE TABLE change_requests (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    created_by_user_id UUID,
    correlation_id VARCHAR(255) NOT NULL,
    source VARCHAR(32) NOT NULL,
    source_ref JSONB NOT NULL,
    kind VARCHAR(32) NOT NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    priority VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE INDEX ix_change_request_project_status ON change_requests (project_id, status);
CREATE INDEX ix_change_requests_correlation_id ON change_requests (correlation_id);

CREATE TABLE clarifications (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    service_account_id UUID NOT NULL,
    recipient_user_id UUID NOT NULL,
    agent_run_id VARCHAR(255) NOT NULL,
    correlation_id VARCHAR(255) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    context TEXT NOT NULL,
    question TEXT NOT NULL,
    expected_answer JSONB,
    status VARCHAR(32) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    telegram_chat_id BIGINT,
    telegram_message_id BIGINT,
    answer_raw TEXT,
    answered_at TIMESTAMP WITH TIME ZONE,
    cancelled_reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(recipient_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(service_account_id) REFERENCES service_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT uq_clarification_idempotency UNIQUE (service_account_id, idempotency_key)
);

CREATE INDEX ix_clarification_pending_expiry ON clarifications (status, expires_at);
CREATE INDEX ix_clarifications_correlation_id ON clarifications (correlation_id);

CREATE TABLE project_memberships (
    project_id UUID NOT NULL,
    user_id UUID NOT NULL,
    role VARCHAR(40) NOT NULL,
    PRIMARY KEY (project_id, user_id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE repositories (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    name VARCHAR(120) NOT NULL,
    ssh_url VARCHAR(1024) NOT NULL,
    default_branch VARCHAR(255) NOT NULL,
    allowed_paths JSONB NOT NULL,
    deploy_key_path VARCHAR(1024),
    known_hosts_path VARCHAR(1024),
    mirror_path VARCHAR(1024),
    current_commit VARCHAR(64),
    status VARCHAR(32) NOT NULL,
    last_synced_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    UNIQUE (project_id, name)
);

CREATE TABLE service_account_projects (
    service_account_id UUID NOT NULL,
    project_id UUID NOT NULL,
    PRIMARY KEY (service_account_id, project_id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(service_account_id) REFERENCES service_accounts (id) ON DELETE CASCADE
);

CREATE TABLE telegram_chats (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    message_thread_id BIGINT,
    kind VARCHAR(32) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT uq_chat_thread UNIQUE NULLS NOT DISTINCT (telegram_chat_id, message_thread_id)
);

CREATE TABLE telegram_identities (
    id UUID NOT NULL,
    user_id UUID NOT NULL,
    telegram_user_id BIGINT NOT NULL,
    username VARCHAR(64),
    private_chat_id BIGINT,
    verified_at TIMESTAMP WITH TIME ZONE,
    reachable BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    UNIQUE (telegram_user_id)
);

CREATE TABLE interactions (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    repository_id UUID,
    correlation_id VARCHAR(255) NOT NULL,
    source VARCHAR(32) NOT NULL,
    source_ref JSONB NOT NULL,
    question TEXT NOT NULL,
    commit_sha VARCHAR(64),
    status VARCHAR(32) NOT NULL,
    answer_markdown TEXT,
    citations JSONB NOT NULL,
    rejected_citations JSONB NOT NULL,
    uncertainty JSONB NOT NULL,
    provider_metadata JSONB NOT NULL,
    error_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(repository_id) REFERENCES repositories (id) ON DELETE SET NULL
);

CREATE INDEX ix_interactions_correlation_id ON interactions (correlation_id);
