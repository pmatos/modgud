"""Fetch an item's extracted source material for model input.

Owns the one path from an item's stored ``format``/``extracted_text_hash``/
``chapters`` to model-ready text, so tier-1 summaries, tier-2 summaries, and
span maps can never drift on what counts as a supported format or on how a
transcript is chunked.
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from modgud.blobs import BlobStore
from modgud.formats import DOCUMENT_FORMATS, TRANSCRIPT_FORMATS, ItemFormat
from modgud.transcripts import TranscriptChunk, chunk_transcript
from modgud.youtube import Chapter


def parse_chapters(chapters_json: object, *, item_id: int) -> tuple[Chapter, ...]:
    """Parse an item's stored chapters JSON into structured chapter markers."""
    if chapters_json is None:
        return ()
    parsed = json.loads(str(chapters_json))
    if not isinstance(parsed, list):
        raise TypeError(f"item {item_id} has malformed chapters")
    return tuple(cast("list[Chapter]", parsed))


def load_transcript_chunks(
    blob_store: BlobStore,
    extracted_text_hash: str,
    chapters_json: object,
    *,
    item_id: int,
) -> tuple[TranscriptChunk, ...]:
    """Load an item's stored transcript blob and split it into chunks.

    Shared by every reader of an item's transcript (span-map generation, the
    transcript page, tier-1 and tier-2 summarization) so their chunk
    boundaries can never drift apart.
    """
    transcript = blob_store.get(extracted_text_hash)
    return chunk_transcript(
        transcript, chapters=parse_chapters(chapters_json, item_id=item_id)
    )


@dataclass(frozen=True, slots=True)
class _ItemSource:
    """The stored columns an item's source material is derived from."""

    item_format: str
    extracted_text_hash: str
    chapters_json: object


def _fetch_item_source(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    accepted_formats: frozenset[ItemFormat],
) -> _ItemSource:
    row = connection.execute(
        "SELECT format, extracted_text_hash, chapters FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"item {item_id} does not exist")
    item_format, extracted_text_hash, chapters_json = row
    if extracted_text_hash is None:
        raise ValueError(f"item {item_id} has no extracted text")
    if item_format not in accepted_formats:
        raise ValueError(f"item {item_id} has no supported extracted text")
    return _ItemSource(
        item_format=str(item_format),
        extracted_text_hash=str(extracted_text_hash),
        chapters_json=chapters_json,
    )


def _nonempty_chunks(
    blob_store: BlobStore, source: _ItemSource, item_id: int
) -> tuple[TranscriptChunk, ...]:
    chunks = load_transcript_chunks(
        blob_store, source.extracted_text_hash, source.chapters_json, item_id=item_id
    )
    if not chunks:
        raise ValueError(f"item {item_id} has no transcript cues")
    return chunks


def fetch_source_texts(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
    *,
    accepted_formats: frozenset[ItemFormat],
) -> tuple[str, ...]:
    """Return one item's extracted text as one or more model-ready texts.

    A document format decodes to a single text; a transcript format splits
    into one text per chunk. ``accepted_formats`` is the caller's own
    eligibility policy — tier-1 accepts every summarizable format, tier-2
    excludes PDF — and is checked before anything is decoded. Independently of
    it, only a known document or transcript format is ever decoded, so a
    format in neither is refused rather than chunked into nothing.
    """
    source = _fetch_item_source(connection, item_id, accepted_formats=accepted_formats)
    if source.item_format in DOCUMENT_FORMATS:
        return (blob_store.get(source.extracted_text_hash).decode("utf-8"),)
    if source.item_format in TRANSCRIPT_FORMATS:
        return tuple(
            chunk.text for chunk in _nonempty_chunks(blob_store, source, item_id)
        )
    raise ValueError(f"item {item_id} has no supported extracted text")


def fetch_transcript_chunks(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
) -> tuple[TranscriptChunk, ...]:
    """Return one item's transcript split into chunks, timings intact.

    For the caller that needs the chunks themselves rather than their text, to
    map model-selected chunk ids back to timestamps. Transcript formats are
    the fixed accepted set, since a document has no chunks.
    """
    source = _fetch_item_source(
        connection, item_id, accepted_formats=TRANSCRIPT_FORMATS
    )
    return _nonempty_chunks(blob_store, source, item_id)
