ALTER TABLE change_requests
    DROP CONSTRAINT ck_change_request_citations,
    DROP CONSTRAINT ck_change_request_requester_profile,
    DROP CONSTRAINT uq_change_request_source_interaction,
    DROP CONSTRAINT fk_change_request_source_interaction,
    DROP COLUMN citations,
    DROP COLUMN agent_summary,
    DROP COLUMN question,
    DROP COLUMN requester_profile,
    DROP COLUMN source_interaction_id;

ALTER TABLE project_memberships
    DROP COLUMN preferred_language;
