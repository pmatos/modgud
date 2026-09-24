"""Behavioral tests for existing-item lifecycle transitions."""

import json
from pathlib import Path

from modgud.database import connect
from modgud.item_lifecycle import mark_extracted


def test_extracted_transition_updates_item_and_matching_event(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', ?, 'web', 'captured', 'example.com')
            """,
            ("a" * 64,),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        mark_extracted(
            connection,
            item_id,
            extracted_text_hash="b" * 64,
            event_source="audio_fallback",
            expected_state="captured",
            title="A durable article",
            author="Ada Rivera",
            item_source="Practical Python",
        )

        item = connection.execute(
            """
            SELECT state, extracted_text_hash, title, author, source
            FROM items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT type, payload FROM events WHERE item_id = ?",
            (item_id,),
        ).fetchone()

    assert item == (
        "extracted",
        "b" * 64,
        "A durable article",
        "Ada Rivera",
        "Practical Python",
    )
    assert event is not None
    assert event[0] == "extracted"
    assert json.loads(event[1]) == {
        "extracted_text_hash": "b" * 64,
        "source": "audio_fallback",
    }
