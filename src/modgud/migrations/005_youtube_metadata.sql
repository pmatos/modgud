BEGIN IMMEDIATE;

ALTER TABLE items ADD COLUMN channel TEXT;
ALTER TABLE items ADD COLUMN chapters TEXT CHECK (
    chapters IS NULL OR json_valid(chapters)
);

PRAGMA user_version = 5;

COMMIT;
