-- DeskScope schema, migration 3: integrations, device sheets and API tokens.

-- Where a ticket came from, and a stable key for tickets opened by a tool
-- (e.g. "pcscope:FIN-LT03:diskSpace") so an import never opens duplicates.
ALTER TABLE tickets ADD COLUMN source TEXT NOT NULL DEFAULT 'web'
    CHECK (source IN ('web', 'api', 'pcscope', 'netscope', 'adflow'));
ALTER TABLE tickets ADD COLUMN external_key TEXT;

-- At most one *active* ticket per key; a problem that comes back after being
-- resolved gets a new ticket.
CREATE UNIQUE INDEX ux_tickets_external_active ON tickets (external_key)
    WHERE external_key IS NOT NULL AND status IN ('new', 'open', 'pending');

-- System accounts (the integrations) open tickets but can never sign in.
ALTER TABLE users ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1));

INSERT INTO users (username, full_name, email, role, password_hash, active, is_system, created_at)
VALUES ('integrations', 'Integrations', 'integrations@deskscope.invalid', 'agent', '!', 1, 1,
        strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

-- Device sheets imported from PCScope (one row per computer).
CREATE TABLE devices (
    name         TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    data         TEXT NOT NULL,          -- JSON object with the inventory summary
    collected_at TEXT,
    imported_at  TEXT NOT NULL
);

-- API tokens: only a SHA-256 of the token is stored; the prefix helps people
-- recognise their tokens in the list.
CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    prefix       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX ix_api_tokens_user ON api_tokens (user_id);
