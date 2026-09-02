BEGIN IMMEDIATE;

CREATE TABLE tier_1_summaries (
    item_id INTEGER PRIMARY KEY REFERENCES items (id) ON DELETE RESTRICT,
    one_liner TEXT NOT NULL CHECK (
        length(trim(one_liner)) > 0
        AND instr(one_liner, char(10)) = 0
        AND instr(one_liner, char(13)) = 0
    ),
    claims TEXT NOT NULL CHECK (
        json_valid(claims)
        AND json_type(claims) = 'array'
        AND json_array_length(claims) BETWEEN 3 AND 5
    ),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

PRAGMA user_version = 6;

COMMIT;
