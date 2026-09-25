-- Application tables. Runs automatically on first container start via
-- /docker-entrypoint-initdb.d, alongside the pgvector extension.

CREATE EXTENSION IF NOT EXISTS vector;

-- Who may ask, and what they may see. `scope` is ANDed into every retrieval
-- filter and every scoped tool - it is the data-access half of security.
CREATE TABLE IF NOT EXISTS app_user (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    scope         TEXT NOT NULL DEFAULT 'public',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS request (
    id          TEXT PRIMARY KEY,
    actor_id    TEXT REFERENCES app_user(id),
    domain      TEXT NOT NULL,
    raw_text    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'received',
    answer      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per plan REVISION, so the approval history is a record rather than a
-- single mutable flag. Approving revision 1 is visibly not approving revision 2.
CREATE TABLE IF NOT EXISTS plan_revision (
    request_id   TEXT NOT NULL REFERENCES request(id) ON DELETE CASCADE,
    revision     INT  NOT NULL,
    steps        JSONB NOT NULL,
    max_risk     TEXT NOT NULL,
    approved_by  TEXT REFERENCES app_user(id),
    approved_at  TIMESTAMPTZ,
    PRIMARY KEY (request_id, revision)
);

-- Everything security-relevant, append-only. This is what the trajectory eval
-- reads, and the answer to "how do you know it was gated?".
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    request_id TEXT,
    stage      TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     JSONB,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_request_idx ON audit_log (request_id, at);
CREATE INDEX IF NOT EXISTS request_actor_idx ON request (actor_id, created_at DESC);
