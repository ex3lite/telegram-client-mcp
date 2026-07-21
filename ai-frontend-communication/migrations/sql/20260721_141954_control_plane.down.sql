DROP TABLE agent_messages;

ALTER TABLE interactions
    DROP CONSTRAINT ck_interaction_privacy_findings,
    DROP CONSTRAINT ck_interaction_artifacts,
    DROP COLUMN privacy_findings,
    DROP COLUMN artifacts;

ALTER TABLE service_accounts
    DROP CONSTRAINT ck_service_account_version,
    DROP COLUMN version;

DROP TABLE system_secrets;
DROP TABLE project_agent_settings;
