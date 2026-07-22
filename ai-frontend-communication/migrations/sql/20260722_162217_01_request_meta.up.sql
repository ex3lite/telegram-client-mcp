ALTER TABLE project_memberships
    ADD COLUMN preferred_language VARCHAR(16) NOT NULL DEFAULT 'ru';

ALTER TABLE change_requests
    ADD COLUMN source_interaction_id UUID,
    ADD COLUMN requester_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN question TEXT NOT NULL DEFAULT '',
    ADD COLUMN agent_summary TEXT NOT NULL DEFAULT '',
    ADD COLUMN citations JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT fk_change_request_source_interaction
        FOREIGN KEY(source_interaction_id) REFERENCES interactions (id) ON DELETE SET NULL,
    ADD CONSTRAINT uq_change_request_source_interaction UNIQUE (source_interaction_id),
    ADD CONSTRAINT ck_change_request_requester_profile
        CHECK (jsonb_typeof(requester_profile) = 'object'),
    ADD CONSTRAINT ck_change_request_citations
        CHECK (jsonb_typeof(citations) = 'array');
