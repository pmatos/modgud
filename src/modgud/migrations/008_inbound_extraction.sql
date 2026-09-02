BEGIN IMMEDIATE;

ALTER TABLE postmark_inbound_messages ADD COLUMN target_url TEXT CHECK (
    target_url IS NULL OR length(trim(target_url)) > 0
);
ALTER TABLE postmark_inbound_messages ADD COLUMN origin TEXT CHECK (
    origin IS NULL OR length(trim(origin)) > 0
);
ALTER TABLE postmark_inbound_messages ADD COLUMN processed_at TEXT;
ALTER TABLE postmark_inbound_messages ADD COLUMN item_id INTEGER
    REFERENCES items (id) ON DELETE RESTRICT;

PRAGMA user_version = 8;

COMMIT;
