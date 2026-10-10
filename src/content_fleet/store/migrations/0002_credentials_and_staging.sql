-- Credentials that change at runtime (refreshed OAuth tokens). The value is
-- AES-GCM ciphertext under a key that is injected as a secret and never
-- stored here.
CREATE TABLE credentials (
    account     TEXT NOT NULL,
    role        TEXT NOT NULL,
    -- SHA-256 of the injected seed this value descends from. A different
    -- seed on disk means the operator supplied a new credential.
    seed_sha256 TEXT NOT NULL,
    ciphertext  BLOB NOT NULL,
    obtained_at REAL NOT NULL,
    expires_at  REAL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (account, role)
);

-- Files currently reachable from the internet through the self-hosted stager.
CREATE TABLE staged_media (
    token      TEXT PRIMARY KEY,
    media_id   TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL
);
