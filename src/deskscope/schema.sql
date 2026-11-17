-- DeskScope schema, version 1.
-- Timestamps are ISO 8601 strings in UTC.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    full_name     TEXT    NOT NULL,
    email         TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('requester', 'agent', 'admin')),
    password_hash TEXT    NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at    TEXT    NOT NULL
);

CREATE TABLE tickets (
    id           INTEGER PRIMARY KEY,
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    category     TEXT    NOT NULL
        CHECK (category IN ('hardware', 'software', 'network', 'access', 'other')),
    priority     TEXT    NOT NULL CHECK (priority IN ('critical', 'high', 'medium', 'low')),
    status       TEXT    NOT NULL
        CHECK (status IN ('new', 'open', 'pending', 'resolved', 'closed')),
    requester_id INTEGER NOT NULL REFERENCES users (id),
    assignee_id  INTEGER REFERENCES users (id),
    device       TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    resolved_at  TEXT,
    closed_at    TEXT
);

CREATE INDEX ix_tickets_status    ON tickets (status);
CREATE INDEX ix_tickets_requester ON tickets (requester_id);
CREATE INDEX ix_tickets_assignee  ON tickets (assignee_id);

CREATE TABLE comments (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES users (id),
    body       TEXT    NOT NULL,
    internal   INTEGER NOT NULL DEFAULT 0 CHECK (internal IN (0, 1)),
    created_at TEXT    NOT NULL
);

CREATE INDEX ix_comments_ticket ON comments (ticket_id);

-- Audit trail: one row per field change ("created", "status", "assignee", ...).
CREATE TABLE ticket_events (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    actor_id   INTEGER REFERENCES users (id),
    field      TEXT    NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX ix_events_ticket ON ticket_events (ticket_id);
