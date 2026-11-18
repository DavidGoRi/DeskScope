-- DeskScope schema, migration 6: tickets from HardenScope (security hardening audits).

-- SQLite cannot change a CHECK constraint, so the tickets table is rebuilt
-- again to accept the new "hardenscope" source (same procedure as migration 5).
CREATE TABLE tickets_new (
    id                   INTEGER PRIMARY KEY,
    title                TEXT    NOT NULL,
    description          TEXT    NOT NULL,
    category             TEXT    NOT NULL
        CHECK (category IN ('hardware', 'software', 'network', 'access', 'other')),
    priority             TEXT    NOT NULL CHECK (priority IN ('critical', 'high', 'medium', 'low')),
    status               TEXT    NOT NULL
        CHECK (status IN ('new', 'open', 'pending', 'resolved', 'closed')),
    requester_id         INTEGER NOT NULL REFERENCES users (id),
    assignee_id          INTEGER REFERENCES users (id),
    device               TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    resolved_at          TEXT,
    closed_at            TEXT,
    impact               TEXT    NOT NULL DEFAULT 'medium' CHECK (impact IN ('high', 'medium', 'low')),
    urgency              TEXT    NOT NULL DEFAULT 'medium' CHECK (urgency IN ('high', 'medium', 'low')),
    first_response_at    TEXT,
    source               TEXT    NOT NULL DEFAULT 'web'
        CHECK (source IN ('web', 'api', 'email', 'pcscope', 'netscope', 'adflow', 'hardenscope')),
    external_key         TEXT,
    satisfaction         INTEGER CHECK (satisfaction BETWEEN 1 AND 5),
    satisfaction_comment TEXT
);

INSERT INTO tickets_new (id, title, description, category, priority, status, requester_id,
    assignee_id, device, created_at, updated_at, resolved_at, closed_at, impact, urgency,
    first_response_at, source, external_key, satisfaction, satisfaction_comment)
SELECT id, title, description, category, priority, status, requester_id,
    assignee_id, device, created_at, updated_at, resolved_at, closed_at, impact, urgency,
    first_response_at, source, external_key, satisfaction, satisfaction_comment
FROM tickets;

DROP TABLE tickets;
ALTER TABLE tickets_new RENAME TO tickets;

CREATE INDEX ix_tickets_status    ON tickets (status);
CREATE INDEX ix_tickets_requester ON tickets (requester_id);
CREATE INDEX ix_tickets_assignee  ON tickets (assignee_id);
CREATE INDEX ix_tickets_priority  ON tickets (priority);
CREATE UNIQUE INDEX ux_tickets_external_active ON tickets (external_key)
    WHERE external_key IS NOT NULL AND status IN ('new', 'open', 'pending');
