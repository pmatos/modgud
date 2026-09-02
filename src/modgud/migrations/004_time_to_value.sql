BEGIN IMMEDIATE;

ALTER TABLE items ADD COLUMN duration_seconds REAL CHECK (duration_seconds >= 0);
ALTER TABLE items ADD COLUMN time_to_value_seconds INTEGER CHECK (
    time_to_value_seconds >= 0
);

PRAGMA user_version = 4;

COMMIT;
