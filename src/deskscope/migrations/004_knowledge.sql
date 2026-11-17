-- DeskScope schema, migration 4: knowledge base and customer satisfaction.

CREATE TABLE articles (
    id               INTEGER PRIMARY KEY,
    title            TEXT    NOT NULL,
    body             TEXT    NOT NULL,
    category         TEXT    NOT NULL
        CHECK (category IN ('hardware', 'software', 'network', 'access', 'other')),
    -- "everyone": self-service for employees; "staff": internal procedures.
    audience         TEXT    NOT NULL DEFAULT 'everyone' CHECK (audience IN ('everyone', 'staff')),
    status           TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
    source_ticket_id INTEGER REFERENCES tickets (id) ON DELETE SET NULL,
    author_id        INTEGER NOT NULL REFERENCES users (id),
    views            INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE INDEX ix_articles_status ON articles (status, audience);

-- Full-text index kept in sync by triggers. "remove_diacritics" lets
-- "impresion" find "impresión".
CREATE VIRTUAL TABLE articles_fts USING fts5(
    title, body, content = 'articles', content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts (rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER articles_au AFTER UPDATE OF title, body ON articles BEGIN
    INSERT INTO articles_fts (articles_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO articles_fts (rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- Satisfaction survey answered by the requester once the ticket is resolved.
ALTER TABLE tickets ADD COLUMN satisfaction INTEGER CHECK (satisfaction BETWEEN 1 AND 5);
ALTER TABLE tickets ADD COLUMN satisfaction_comment TEXT;
