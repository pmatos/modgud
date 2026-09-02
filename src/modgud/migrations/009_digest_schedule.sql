BEGIN IMMEDIATE;

CREATE TABLE digest_schedule (
    local_date TEXT PRIMARY KEY CHECK (local_date = date(local_date)),
    outcome TEXT NOT NULL CHECK (outcome IN ('empty', 'sent')),
    completed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

PRAGMA user_version = 9;

COMMIT;
