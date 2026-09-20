BEGIN IMMEDIATE;

ALTER TABLE items ADD COLUMN page_url TEXT CHECK (
    page_url IS NULL OR length(trim(page_url)) > 0
);

UPDATE items
SET page_url = (
    SELECT json_extract(events.payload, '$.url')
    FROM events
    WHERE events.item_id = items.id
      AND events.type = 'captured'
    ORDER BY coalesce(
        json_extract(events.payload, '$.url')
            = json_extract(events.payload, '$.feed_url'),
        0
    ), events.id DESC
    LIMIT 1
)
WHERE canonical_url LIKE 'podcast:%';

PRAGMA user_version = 12;

COMMIT;
