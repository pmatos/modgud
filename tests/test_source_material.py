"""Behavior at the Item-id source-material interface."""

import json
import sqlite3
from pathlib import Path

import pytest

from modgud.blobs import BlobStore
from modgud.database import connect
from modgud.formats import TRANSCRIPT_FORMATS, ItemFormat
from modgud.source_material import (
    fetch_source_texts,
    fetch_transcript_chunks,
    get_transcript_chunks,
)

_TRANSCRIPT = b"""WEBVTT

00:00:00.000 --> 00:00:02.000
Opening context.

00:00:10.000 --> 00:00:12.000
A useful technique.
"""


def _insert_item(
    connection: sqlite3.Connection,
    *,
    item_format: ItemFormat = ItemFormat.YOUTUBE,
    extracted_text_hash: str | None,
    chapters: str | None = None,
) -> int:
    item_id = connection.execute(
        """
        INSERT INTO items (
            canonical_url, content_hash, extracted_text_hash,
            format, state, source, chapters
        ) VALUES ('https://example.com/item', 'raw-content', ?, ?,
                  'extracted', 'example.com', ?)
        """,
        (extracted_text_hash, item_format, chapters),
    ).lastrowid
    assert item_id is not None
    return item_id


@pytest.mark.parametrize("item_format", sorted(TRANSCRIPT_FORMATS))
def test_display_and_model_readers_share_chapter_sensitive_transcript_chunks(
    tmp_path: Path, item_format: ItemFormat
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(_TRANSCRIPT)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = _insert_item(
            connection,
            item_format=item_format,
            extracted_text_hash=transcript_hash,
            chapters=json.dumps(
                [{"start_time": 10.0, "end_time": 12.0, "title": "Technique"}]
            ),
        )
        changes_before = connection.total_changes

        chunks = get_transcript_chunks(connection, blob_store, item_id)

        assert chunks is not None
        assert [(chunk.start_ms, chunk.end_ms, chunk.text) for chunk in chunks] == [
            (0, 2_000, "Opening context."),
            (10_000, 12_000, "A useful technique."),
        ]
        assert fetch_transcript_chunks(connection, blob_store, item_id) == chunks
        assert fetch_source_texts(
            connection, blob_store, item_id, accepted_formats=TRANSCRIPT_FORMATS
        ) == ("Opening context.", "A useful technique.")
        assert connection.total_changes == changes_before


def test_empty_transcript_is_displayable_but_remains_invalid_model_input(
    tmp_path: Path,
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(b"WEBVTT\n\n")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = _insert_item(connection, extracted_text_hash=transcript_hash)

        assert get_transcript_chunks(connection, blob_store, item_id) == ()
        with pytest.raises(ValueError, match=f"item {item_id} has no transcript cues"):
            fetch_transcript_chunks(connection, blob_store, item_id)
        with pytest.raises(ValueError, match=f"item {item_id} has no transcript cues"):
            fetch_source_texts(
                connection, blob_store, item_id, accepted_formats=TRANSCRIPT_FORMATS
            )


@pytest.mark.parametrize(
    ("failure", "error_type", "message"),
    [
        ("missing-item", ValueError, "does not exist"),
        ("missing-text", ValueError, "has no extracted text"),
        ("document", ValueError, "has no supported extracted text"),
        ("pdf", ValueError, "has no supported extracted text"),
        ("invalid-hash", ValueError, "blob hash must be a lowercase SHA-256 digest"),
        ("missing-blob", FileNotFoundError, "No such file"),
        ("malformed-chapters", TypeError, "has malformed chapters"),
        ("invalid-utf8", UnicodeDecodeError, "codec can't decode"),
    ],
)
def test_unavailable_display_transcript_preserves_strict_reader_failure(
    tmp_path: Path, failure: str, error_type: type[Exception], message: str
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash: str | None = blob_store.put(_TRANSCRIPT)
    item_format = ItemFormat.YOUTUBE
    chapters = None
    if failure == "missing-text":
        transcript_hash = None
    elif failure in {"document", "pdf"}:
        item_format = ItemFormat.WEB if failure == "document" else ItemFormat.PDF
        # Both must be refused before reading a blob or interpreting chapters.
        transcript_hash = "invalid-hash"
        chapters = json.dumps({"not": "a list"})
    elif failure == "invalid-hash":
        transcript_hash = "invalid-hash"
    elif failure == "missing-blob":
        transcript_hash = "f" * 64
    elif failure == "malformed-chapters":
        chapters = json.dumps({"not": "a list"})
    elif failure == "invalid-utf8":
        transcript_hash = blob_store.put(b"\xff")

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = (
            404
            if failure == "missing-item"
            else _insert_item(
                connection,
                item_format=item_format,
                extracted_text_hash=transcript_hash,
                chapters=chapters,
            )
        )

        assert get_transcript_chunks(connection, blob_store, item_id) is None
        with pytest.raises(error_type, match=message):
            fetch_transcript_chunks(connection, blob_store, item_id)


def test_optional_transcript_read_does_not_hide_database_failures(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "modgud.sqlite3")
    connection.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        get_transcript_chunks(connection, BlobStore(tmp_path / "blobs"), 1)
