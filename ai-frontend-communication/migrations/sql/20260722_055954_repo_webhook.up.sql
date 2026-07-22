ALTER TABLE repositories
    ADD COLUMN github_repository VARCHAR(255),
    ADD COLUMN auto_sync_enabled BOOLEAN DEFAULT false NOT NULL,
    ADD COLUMN sync_generation BIGINT DEFAULT 0 NOT NULL,
    ADD COLUMN last_webhook_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN last_webhook_commit VARCHAR(64),
    ADD CONSTRAINT uq_repositories_project_github_repository
        UNIQUE (project_id, github_repository),
    ADD CONSTRAINT ck_repository_auto_sync_source
        CHECK (NOT auto_sync_enabled OR github_repository IS NOT NULL),
    ADD CONSTRAINT ck_repository_sync_generation CHECK (sync_generation >= 0);
