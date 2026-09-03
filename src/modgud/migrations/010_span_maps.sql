BEGIN IMMEDIATE;

CREATE TABLE span_maps (
    item_id INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE span_map_spans (
    item_id INTEGER NOT NULL REFERENCES span_maps (item_id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
    description TEXT NOT NULL CHECK (
        length(trim(description)) > 0
        AND instr(description, char(10)) = 0
        AND instr(description, char(13)) = 0
    ),
    PRIMARY KEY (item_id, position)
);

PRAGMA user_version = 10;

COMMIT;
