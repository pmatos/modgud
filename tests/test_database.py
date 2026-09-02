"""Behavioral tests for the durable SQLite store."""

import sqlite3
from pathlib import Path

import pytest

from modgud.database import apply_migrations, connect


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
