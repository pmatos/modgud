BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'captured',
            'extracted',
            'summarized',
            'unsummarizable',
            'failed'
        )
    ),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items (id) ON DELETE RESTRICT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TRIGGER IF NOT EXISTS events_reject_updates
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_reject_deletes
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_reject_replacements
BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

PRAGMA user_version = 1;

COMMIT;
