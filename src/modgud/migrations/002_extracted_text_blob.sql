BEGIN IMMEDIATE;

ALTER TABLE items ADD COLUMN extracted_text_hash TEXT;

PRAGMA user_version = 2;

COMMIT;
