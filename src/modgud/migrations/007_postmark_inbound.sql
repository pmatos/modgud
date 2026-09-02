BEGIN IMMEDIATE;

CREATE TABLE postmark_inbound_messages (
    message_id TEXT PRIMARY KEY CHECK (length(trim(message_id)) > 0),
    payload TEXT NOT NULL CHECK (
        json_valid(payload)
        AND json_type(payload) = 'object'
    ),
    queued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE postmark_inbound_poll_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    completed_at TEXT NOT NULL
);

PRAGMA user_version = 7;

COMMIT;
