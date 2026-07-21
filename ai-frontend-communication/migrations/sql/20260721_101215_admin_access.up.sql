CREATE TABLE admin_principals (
    id UUID NOT NULL,
    name VARCHAR(160) NOT NULL,
    active BOOLEAN NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_admin_principal_name UNIQUE (name)
);

CREATE TABLE admin_access_keys (
    id UUID NOT NULL,
    principal_id UUID NOT NULL,
    fingerprint BYTEA NOT NULL,
    active BOOLEAN NOT NULL,
    last_used_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(principal_id) REFERENCES admin_principals (id) ON DELETE CASCADE,
    CONSTRAINT uq_admin_access_key_fingerprint UNIQUE (fingerprint)
);

CREATE TABLE admin_sessions (
    id UUID NOT NULL,
    access_key_id UUID NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(access_key_id) REFERENCES admin_access_keys (id) ON DELETE CASCADE
);

CREATE INDEX ix_admin_session_access_key ON admin_sessions (access_key_id);
