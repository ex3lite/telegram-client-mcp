ALTER TABLE project_memberships
    ADD COLUMN knowledge_scope VARCHAR(24) NOT NULL DEFAULT 'integration',
    ADD COLUMN can_create_requests BOOLEAN NOT NULL DEFAULT TRUE,
    ADD CONSTRAINT ck_project_membership_knowledge_scope
        CHECK (knowledge_scope IN ('integration', 'internal'));

UPDATE project_memberships
SET knowledge_scope = 'internal'
WHERE lower(trim(role)) IN ('owner', 'admin', 'backend_admin')
   OR lower(trim(coalesce(department, ''))) = 'backend';

ALTER TABLE conversation_threads
    ADD COLUMN claude_session_id UUID,
    ADD COLUMN claude_repository_id UUID,
    ADD COLUMN claude_commit_sha VARCHAR(64),
    ADD COLUMN claude_policy_hash CHAR(64),
    ADD COLUMN claude_compaction_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN claude_last_compacted_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN claude_context_validated_at TIMESTAMP WITH TIME ZONE,
    ADD CONSTRAINT fk_conversation_thread_claude_repository
        FOREIGN KEY(claude_repository_id) REFERENCES repositories (id) ON DELETE SET NULL,
    ADD CONSTRAINT ck_conversation_thread_compaction_count
        CHECK (claude_compaction_count >= 0);
