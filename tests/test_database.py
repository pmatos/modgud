"""Behavioral tests for the durable SQLite store."""

import sqlite3
from pathlib import Path

import pytest

from modgud.database import _MIGRATIONS, apply_migrations, connect, transaction


def test_owned_transaction_initializes_commits_and_closes(tmp_path: Path) -> None:
    database = tmp_path / "modgud.sqlite3"
    with transaction(database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA user_version").fetchone() == (
            len(_MIGRATIONS),
        )
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/committed', 'content', 'web',
                    'captured', 'example.com')
            """
        )
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    with transaction(database) as reopened:
        assert reopened.execute("SELECT canonical_url FROM items").fetchall() == [
            ("https://example.com/committed",)
        ]


@pytest.mark.parametrize(
    "error", [RuntimeError("abort"), KeyboardInterrupt(), SystemExit(17)]
)
def test_owned_transaction_rolls_back_and_closes_on_body_failure(
    tmp_path: Path, error: BaseException
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with pytest.raises(type(error)) as caught, transaction(database) as connection:
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/aborted', 'content', 'web',
                    'captured', 'example.com')
            """
        )
        raise error

    assert caught.value is error
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    with transaction(database) as reopened:
        assert reopened.execute("SELECT count(*) FROM items").fetchone() == (0,)


def test_owned_transaction_closes_and_rolls_back_after_commit_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        transaction(database) as connection,
    ):
        connection.execute(
            """
            CREATE TABLE deferred_references (
                item_id INTEGER REFERENCES items(id) DEFERRABLE INITIALLY DEFERRED
            )
            """
        )
        connection.execute("INSERT INTO deferred_references VALUES (999)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    with transaction(database) as reopened:
        assert reopened.execute(
            "SELECT count(*) FROM deferred_references"
        ).fetchone() == (0,)


def test_owned_transaction_keeps_explicit_locking_with_the_caller(
    tmp_path: Path,
) -> None:
    with transaction(tmp_path / "modgud.sqlite3") as connection:
        assert not connection.in_transaction
        connection.execute("BEGIN IMMEDIATE")
        assert connection.in_transaction


def test_owned_transaction_commits_and_closes_on_early_return(tmp_path: Path) -> None:
    database = tmp_path / "modgud.sqlite3"

    def insert_and_return() -> sqlite3.Connection:
        with transaction(database) as connection:
            connection.execute(
                """
                INSERT INTO items (canonical_url, content_hash, format, state, source)
                VALUES ('https://example.com/returned', 'content', 'web',
                        'captured', 'example.com')
                """
            )
            return connection

    closed = insert_and_return()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        closed.execute("SELECT 1")
    with transaction(database) as reopened:
        assert reopened.execute("SELECT canonical_url FROM items").fetchall() == [
            ("https://example.com/returned",)
        ]


def test_raw_connection_remains_reusable_after_its_transaction_block(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "modgud.sqlite3")
    try:
        connection.execute("CREATE TABLE raw_values (value TEXT)")
        with connection:
            connection.execute("INSERT INTO raw_values VALUES ('committed')")
        with pytest.raises(RuntimeError), connection:
            connection.execute("INSERT INTO raw_values VALUES ('rolled back')")
            raise RuntimeError("abort")

        assert connection.execute("SELECT value FROM raw_values").fetchall() == [
            ("committed",)
        ]
    finally:
        connection.close()


def test_opening_a_new_database_creates_the_durable_store(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }

    assert {"items", "events"} <= tables


def test_items_store_identity_format_state_source_and_timestamps(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "https://example.com/article",
                "a" * 64,
                "web",
                "captured",
                "example.com",
            ),
        )
        item = connection.execute(
            """
            SELECT canonical_url, content_hash, format, state, source,
                   created_at IS NOT NULL, updated_at IS NOT NULL
            FROM items
            """
        ).fetchone()

    assert item == (
        "https://example.com/article",
        "a" * 64,
        "web",
        "captured",
        "example.com",
        1,
        1,
    )


def test_items_store_extracted_web_metadata(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (
                canonical_url,
                content_hash,
                extracted_text_hash,
                format,
                state,
                source,
                title,
                author
            ) VALUES (?, ?, ?, 'web', 'extracted', ?, ?, ?)
            """,
            (
                "https://example.com/article",
                "a" * 64,
                "b" * 64,
                "Example Journal",
                "A Useful Article",
                "Ada Rivera",
            ),
        )
        metadata = connection.execute(
            "SELECT title, author, source, extracted_text_hash FROM items"
        ).fetchone()

    assert metadata == (
        "A Useful Article",
        "Ada Rivera",
        "Example Journal",
        "b" * 64,
    )


def test_items_store_duration_and_time_to_value_estimates(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (
                canonical_url,
                content_hash,
                format,
                state,
                source,
                duration_seconds,
                time_to_value_seconds
            ) VALUES (?, ?, 'youtube', 'captured', ?, ?, ?)
            """,
            (
                "https://www.youtube.com/watch?v=example",
                "a" * 64,
                "Example Channel",
                90.2,
                91,
            ),
        )
        stored = connection.execute(
            "SELECT duration_seconds, time_to_value_seconds FROM items"
        ).fetchone()

    assert stored == (90.2, 91)


def test_item_state_is_limited_to_the_lifecycle(tmp_path: Path) -> None:
    allowed_states = (
        "captured",
        "extracted",
        "summarized",
        "unsummarizable",
        "failed",
    )

    with connect(tmp_path / "modgud.sqlite3") as connection:
        for position, state in enumerate(allowed_states):
            connection.execute(
                """
                INSERT INTO items
                    (canonical_url, content_hash, format, state, source)
                VALUES (?, ?, 'web', ?, 'example.com')
                """,
                (f"https://example.com/{position}", str(position), state),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO items
                    (canonical_url, content_hash, format, state, source)
                VALUES ('https://example.com/invalid', 'invalid', 'web',
                        'pending', 'example.com')
                """
            )

        stored_states = {
            row[0] for row in connection.execute("SELECT state FROM items")
        }

    assert stored_states == set(allowed_states)


def test_item_identities_prevent_duplicate_content(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'same-content', 'web',
                    'captured', 'example.com')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO items
                    (canonical_url, content_hash, format, state, source)
                VALUES ('https://example.com/article', 'other-content', 'web',
                        'captured', 'example.com')
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO items
                    (canonical_url, content_hash, format, state, source)
                VALUES ('https://mirror.example/article', 'same-content', 'web',
                        'captured', 'mirror.example')
                """
            )

        item_count = connection.execute("SELECT count(*) FROM items").fetchone()[0]

    assert item_count == 1


def test_events_store_item_reference_type_payload_and_timestamp(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'content', 'web',
                    'captured', 'example.com')
            """
        )
        item_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO events (item_id, type, payload)
            VALUES (?, 'captured', '{"origin":"manual"}')
            """,
            (item_id,),
        )
        event = connection.execute(
            """
            SELECT item_id, type, payload, created_at IS NOT NULL
            FROM events
            """
        ).fetchone()

    assert event == (item_id, "captured", '{"origin":"manual"}', 1)


def test_an_event_must_reference_an_existing_item(tmp_path: Path) -> None:
    with (
        connect(tmp_path / "modgud.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            """
            INSERT INTO events (item_id, type, payload)
            VALUES (404, 'captured', '{}')
            """
        )


def test_event_payload_must_be_valid_json(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'content', 'web',
                    'captured', 'example.com')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO events (item_id, type, payload)
                VALUES (?, 'captured', 'not JSON')
                """,
                (cursor.lastrowid,),
            )


def test_events_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'content', 'web',
                    'captured', 'example.com')
            """
        )
        event = connection.execute(
            """
            INSERT INTO events (item_id, type, payload)
            VALUES (?, 'captured', '{}')
            """,
            (item.lastrowid,),
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE events SET type = 'changed' WHERE id = ?",
                (event.lastrowid,),
            )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM events WHERE id = ?",
                (event.lastrowid,),
            )

        stored_event = connection.execute(
            "SELECT type, payload FROM events WHERE id = ?",
            (event.lastrowid,),
        ).fetchone()

    assert stored_event == ("captured", "{}")


def test_events_cannot_be_replaced(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'content', 'web',
                    'captured', 'example.com')
            """
        )
        event = connection.execute(
            """
            INSERT INTO events (item_id, type, payload)
            VALUES (?, 'captured', '{}')
            """,
            (item.lastrowid,),
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                INSERT OR REPLACE INTO events (id, item_id, type, payload)
                VALUES (?, ?, 'changed', '{}')
                """,
                (event.lastrowid, item.lastrowid),
            )

        stored_type = connection.execute(
            "SELECT type FROM events WHERE id = ?",
            (event.lastrowid,),
        ).fetchone()[0]

    assert stored_type == "captured"


def test_applying_migrations_twice_is_a_no_op(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'content', 'web',
                    'captured', 'example.com')
            """
        )

        apply_migrations(connection)
        migration_left_transaction_untouched = connection.in_transaction
        connection.rollback()
        item_count = connection.execute("SELECT count(*) FROM items").fetchone()[0]

    assert migration_left_transaction_untouched
    assert item_count == 0


def test_page_url_backfill_prefers_a_captured_page_over_the_feed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    legacy = sqlite3.connect(database)
    for migration in _MIGRATIONS[:11]:
        legacy.executescript(migration.read_text(encoding="utf-8"))
    for canonical_url, content_hash in (
        ("podcast:abc/from-page", "one"),
        ("podcast:abc/from-feed", "two"),
        ("https://example.com/article", "three"),
    ):
        legacy.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES (?, ?, 'podcast', 'captured', 'example.com')
            """,
            (canonical_url, content_hash),
        )
    feed = "https://example.com/feed.xml"
    page = "https://example.com/posts/one"
    for item_id, url in ((1, page), (1, feed), (2, feed), (3, page)):
        legacy.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', ?)",
            (item_id, f'{{"feed_url":"{feed}","url":"{url}"}}'),
        )
    legacy.commit()
    legacy.close()

    with connect(database) as connection:
        page_urls = connection.execute(
            "SELECT canonical_url, page_url FROM items ORDER BY id"
        ).fetchall()

    assert page_urls == [
        ("podcast:abc/from-page", page),
        ("podcast:abc/from-feed", feed),
        ("https://example.com/article", None),
    ]


def test_page_url_backfill_recognizes_a_feed_resubmitted_with_a_trailing_slash(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    legacy = sqlite3.connect(database)
    for migration in _MIGRATIONS[:11]:
        legacy.executescript(migration.read_text(encoding="utf-8"))
    legacy.execute(
        """
        INSERT INTO items (canonical_url, content_hash, format, state, source)
        VALUES ('podcast:abc/guid', 'one', 'podcast', 'captured', 'example.com')
        """
    )
    feed = "https://example.com/feed.xml"
    page = "https://example.com/episodes/queues"
    # An older capture recorded the genuine episode page; a later recapture
    # resubmitted the feed URL itself with a cosmetic trailing slash, which
    # must not be mistaken for a second, distinct page URL.
    for url in (page, f"{feed}/"):
        legacy.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (1, 'captured', ?)",
            (f'{{"feed_url":"{feed}","url":"{url}"}}',),
        )
    legacy.commit()
    legacy.close()

    with connect(database) as connection:
        page_url = connection.execute(
            "SELECT page_url FROM items WHERE id = 1"
        ).fetchone()[0]

    assert page_url == page
