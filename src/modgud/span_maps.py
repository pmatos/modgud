"""Generate and persist timestamped maps of valuable transcript spans."""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, cast

from modgud.blobs import BlobStore
from modgud.config import Settings
from modgud.formats import TRANSCRIPT_FORMATS
from modgud.models import create_model_client
from modgud.transcripts import TranscriptChunk, chunk_transcript
from modgud.youtube import Chapter

_SYSTEM_PROMPT = """Select the transcript chunks worth engaging with.
Return one JSON object with exactly one field, "chunks", containing an array of
objects with exactly these fields:
- "id": the supplied id of a worthwhile chunk
- "description": one line explaining the value in that chunk

Select only supplied chunk ids. Use only the supplied chunk text. Do not add
markdown or commentary."""


@dataclass(frozen=True, slots=True)
class Span:
    """One worthwhile interval with timing derived from transcript chunks."""

    start_ms: int
    end_ms: int
    description: str


@dataclass(frozen=True, slots=True)
class SpanMap:
    """The ordered span-map artifact for one item."""

    spans: tuple[Span, ...]


def _parse_selections(content: str) -> tuple[tuple[str, str], ...]:
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or set(parsed) != {"chunks"}:
        raise ValueError("span map must contain exactly chunks")
    chunks = cast("dict[str, Any]", parsed)["chunks"]
    if not isinstance(chunks, list):
        raise TypeError("chunks must be an array")
    selections: list[tuple[str, str]] = []
    selected_ids: set[str] = set()
    for selection in chunks:
        if not isinstance(selection, dict) or set(selection) != {"id", "description"}:
            raise ValueError("each selected chunk must contain id and description")
        fields = cast("dict[str, Any]", selection)
        chunk_id = fields["id"]
        description = fields["description"]
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            raise ValueError("chunk id must be a non-empty string")
        if (
            not isinstance(description, str)
            or not description.strip()
            or "\n" in description
            or "\r" in description
        ):
            raise ValueError("description must be one non-empty line")
        normalized_id = chunk_id.strip()
        if normalized_id in selected_ids:
            raise ValueError("a chunk id may only be selected once")
        selected_ids.add(normalized_id)
        selections.append((normalized_id, description.strip()))
    return tuple(selections)


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
    transcript page) so their chunk boundaries can never drift apart.
    """
    transcript = blob_store.get(extracted_text_hash)
    return chunk_transcript(
        transcript,
        chapters=parse_chapters(chapters_json, item_id=item_id),
    )


def _resolve_spans(
    chunks: tuple[TranscriptChunk, ...], selections: tuple[tuple[str, str], ...]
) -> SpanMap:
    positions = {chunk.id: position for position, chunk in enumerate(chunks)}
    selected: list[tuple[int, str]] = []
    for chunk_id, description in selections:
        position = positions.get(chunk_id)
        if position is None:
            raise ValueError(f"unknown chunk id: {chunk_id}")
        selected.append((position, description))
    selected.sort(key=lambda selection: selection[0])

    spans: list[Span] = []
    previous_position: int | None = None
    for position, description in selected:
        chunk = chunks[position]
        if previous_position is not None and position == previous_position + 1:
            previous = spans[-1]
            spans[-1] = Span(
                start_ms=previous.start_ms,
                end_ms=chunk.end_ms,
                description=f"{previous.description} {description}",
            )
        else:
            spans.append(
                Span(
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    description=description,
                )
            )
        previous_position = position
    return SpanMap(spans=tuple(spans))


def _store_span_map(
    connection: sqlite3.Connection, item_id: int, span_map: SpanMap
) -> None:
    connection.execute(
        """
        INSERT INTO span_maps (item_id)
        VALUES (?)
        ON CONFLICT (item_id) DO UPDATE SET
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (item_id,),
    )
    connection.execute("DELETE FROM span_map_spans WHERE item_id = ?", (item_id,))
    connection.executemany(
        """
        INSERT INTO span_map_spans (
            item_id, position, start_ms, end_ms, description
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            (item_id, position, span.start_ms, span.end_ms, span.description)
            for position, span in enumerate(span_map.spans)
        ),
    )


def get_span_map(connection: sqlite3.Connection, item_id: int) -> SpanMap | None:
    """Return the stored span map for an item, if it has been generated."""
    exists = connection.execute(
        "SELECT 1 FROM span_maps WHERE item_id = ?", (item_id,)
    ).fetchone()
    if exists is None:
        return None
    rows = connection.execute(
        """
        SELECT start_ms, end_ms, description
        FROM span_map_spans
        WHERE item_id = ?
        ORDER BY position
        """,
        (item_id,),
    ).fetchall()
    return SpanMap(
        spans=tuple(
            Span(start_ms=start_ms, end_ms=end_ms, description=description)
            for start_ms, end_ms, description in rows
        )
    )


def generate_span_map(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
    *,
    settings: Settings,
) -> SpanMap | None:
    """Generate and replace the structured span map for one AV item."""
    item = connection.execute(
        "SELECT format, extracted_text_hash, chapters FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise ValueError(f"item {item_id} does not exist")
    item_format, extracted_text_hash, chapters_json = item
    if item_format not in TRANSCRIPT_FORMATS:
        raise ValueError(f"item {item_id} has no supported transcript")
    if extracted_text_hash is None:
        raise ValueError(f"item {item_id} has no extracted text")
    chunks = load_transcript_chunks(
        blob_store, str(extracted_text_hash), chapters_json, item_id=item_id
    )
    if not chunks:
        raise ValueError(f"item {item_id} has no transcript cues")

    model_input = json.dumps(
        {"chunks": [{"id": chunk.id, "text": chunk.text} for chunk in chunks]},
        ensure_ascii=False,
    )
    routed = create_model_client("span_map", settings=settings)
    span_map = None
    try:
        for _attempt in range(2):
            completion = routed.client.chat.completions.create(
                model=routed.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": model_input},
                ],
                response_format={"type": "json_object"},
            )
            try:
                content = completion.choices[0].message.content
                if not isinstance(content, str):
                    raise TypeError("model returned no span-map content")
                span_map = _resolve_spans(chunks, _parse_selections(content))
            except (IndexError, TypeError, ValueError):
                continue
            break
    finally:
        routed.client.close()

    if span_map is None:
        return None
    _store_span_map(connection, item_id, span_map)
    return span_map
