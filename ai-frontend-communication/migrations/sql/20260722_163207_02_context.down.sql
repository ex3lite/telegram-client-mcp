ALTER TABLE conversation_threads
    DROP CONSTRAINT ck_conversation_thread_compaction_count,
    DROP CONSTRAINT fk_conversation_thread_claude_repository,
    DROP COLUMN claude_context_validated_at,
    DROP COLUMN claude_last_compacted_at,
    DROP COLUMN claude_compaction_count,
    DROP COLUMN claude_policy_hash,
    DROP COLUMN claude_commit_sha,
    DROP COLUMN claude_repository_id,
    DROP COLUMN claude_session_id;

ALTER TABLE project_memberships
    DROP CONSTRAINT ck_project_membership_knowledge_scope,
    DROP COLUMN can_create_requests,
    DROP COLUMN knowledge_scope;
