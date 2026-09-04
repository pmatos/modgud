BEGIN IMMEDIATE;

CREATE TABLE tier_2_summaries (
    item_id INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed')),
    summary_text TEXT CHECK (
        summary_text IS NULL OR length(trim(summary_text)) > 0
    ),
    error TEXT CHECK (error IS NULL OR length(trim(error)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (
        (status = 'pending' AND summary_text IS NULL AND error IS NULL)
        OR (status = 'completed' AND summary_text IS NOT NULL AND error IS NULL)
        OR (status = 'failed' AND summary_text IS NULL AND error IS NOT NULL)
    )
);

PRAGMA user_version = 11;

COMMIT;
