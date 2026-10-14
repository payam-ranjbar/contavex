-- A publication is also its own job: while run_after is set it is due for
-- the worker; a terminal publication has run_after NULL.
CREATE TABLE publications (
    id              TEXT PRIMARY KEY,
    consumer        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    account         TEXT NOT NULL,
    platform        TEXT NOT NULL,
    implementation  TEXT NOT NULL,
    status          TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    -- Provider-owned checkpoint (container ids, upload handles...). Opaque to the core.
    state_json      TEXT NOT NULL DEFAULT '{}',
    result_json     TEXT,
    error_json      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    run_after       REAL,
    lease_until     REAL,
    -- Set while a provider step is executing. Still set after a restart means
    -- the step was interrupted and its outcome is not known.
    in_flight       INTEGER NOT NULL DEFAULT 0,
    deadline        REAL NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (consumer, idempotency_key)
);

CREATE INDEX publications_due ON publications (run_after) WHERE run_after IS NOT NULL;

CREATE TABLE media (
    id           TEXT PRIMARY KEY,
    consumer     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    filename     TEXT,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL
);

CREATE INDEX media_expiry ON media (expires_at);
