-- DeskScope schema, migration 2: ITIL classification and SLA tracking.
--
-- impact  : how many people are affected (high = company/site, medium = team, low = one person)
-- urgency : how fast it hurts (high = work stopped, medium = degraded, low = can wait)
-- priority is derived from both with the matrix in models.py.
-- first_response_at: first public answer from the service desk (or the fix itself).

ALTER TABLE tickets ADD COLUMN impact TEXT NOT NULL DEFAULT 'medium'
    CHECK (impact IN ('high', 'medium', 'low'));
ALTER TABLE tickets ADD COLUMN urgency TEXT NOT NULL DEFAULT 'medium'
    CHECK (urgency IN ('high', 'medium', 'low'));
ALTER TABLE tickets ADD COLUMN first_response_at TEXT;

-- Existing tickets get the impact/urgency pair that gives back their priority.
UPDATE tickets SET impact = 'high',   urgency = 'high'   WHERE priority = 'critical';
UPDATE tickets SET impact = 'medium', urgency = 'high'   WHERE priority = 'high';
UPDATE tickets SET impact = 'medium', urgency = 'medium' WHERE priority = 'medium';
UPDATE tickets SET impact = 'low',    urgency = 'low'    WHERE priority = 'low';

-- First response = earliest public staff comment or resolution.
UPDATE tickets SET first_response_at = (
    SELECT MIN(at) FROM (
        SELECT c.created_at AS at FROM comments c JOIN users u ON u.id = c.author_id
        WHERE c.ticket_id = tickets.id AND c.internal = 0 AND u.role IN ('agent', 'admin')
        UNION ALL
        SELECT tickets.resolved_at
    )
);

CREATE INDEX ix_tickets_priority ON tickets (priority);
