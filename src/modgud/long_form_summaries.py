"""Generate and persist on-demand tier-2 long-form summaries."""

import sqlite3
from dataclasses import dataclass
from typing import Literal

from modgud.blobs import BlobStore
from modgud.config import Settings
from modgud.formats import TRANSCRIPT_FORMATS, ItemFormat
from modgud.models import RoutedModelClient, create_model_client
from modgud.source_material import fetch_source_texts

_SYSTEM_PROMPT = """You write full-length summaries of saved items for someone
who wants the substance without reading the whole source. Write a thorough
prose summary of the supplied text: walk through its structure, explain its
reasoning, and preserve specific details and examples. Do not add headings,
markdown, or commentary about the summarization task itself."""

_SectionStatus = Literal["pending", "completed", "failed"]

# Formats whose extracted text a long-form summary can be generated from.
LONG_FORM_SUMMARY_FORMATS = frozenset({ItemFormat.WEB}) | TRANSCRIPT_FORMATS


@dataclass(frozen=True, slots=True)
class Tier2Summary:
    """The stored state of one item's on-demand long-form summary."""

    status: _SectionStatus
    summary_text: str | None
    error: str | None


def _request_section(routed: RoutedModelClient, source_text: str) -> str | None:
    for _attempt in range(2):
        completion = routed.client.chat.completions.create(
            model=routed.model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": source_text},
            ],
        )
        try:
            content = completion.choices[0].message.content
            if not isinstance(content, str):
                raise TypeError("model returned no summary content")
            section = content.strip()
            if not section:
                raise ValueError("model returned an empty summary")
        except (IndexError, TypeError, ValueError):
            continue
        return section
    return None


def _store_result(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    status: _SectionStatus,
    summary_text: str | None = None,
    error: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO tier_2_summaries (item_id, status, summary_text, error)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (item_id) DO UPDATE SET
            status = excluded.status,
            summary_text = excluded.summary_text,
            error = excluded.error,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (item_id, status, summary_text, error),
    )


def get_long_form_summary(
    connection: sqlite3.Connection, item_id: int
) -> Tier2Summary | None:
    """Return the stored long-form summary state for an item, if requested."""
    row = connection.execute(
        "SELECT status, summary_text, error FROM tier_2_summaries WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    status, summary_text, error = row
    return Tier2Summary(status=status, summary_text=summary_text, error=error)


def request_long_form_summary(connection: sqlite3.Connection, item_id: int) -> bool:
    """Mark an item's long-form summary pending unless one is already underway
    or already stored.

    A prior failure is retried; a pending or completed summary is left alone.
    Returns whether this call is the one that started generation, so a caller
    knows whether to schedule the work.
    """
    cursor = connection.execute(
        """
        INSERT INTO tier_2_summaries (item_id, status)
        VALUES (?, 'pending')
        ON CONFLICT (item_id) DO UPDATE SET
            status = 'pending',
            summary_text = NULL,
            error = NULL,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE tier_2_summaries.status = 'failed'
        """,
        (item_id,),
    )
    return cursor.rowcount > 0


def generate_long_form_summary(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
    *,
    settings: Settings,
) -> str | None:
    """Generate and store the full-length summary for one extracted item.

    Long inputs reuse the same timestamped chunker as tier-1 and span maps.
    Each chunk gets its own long-form section; sections are concatenated in
    order rather than condensed further, since the point of tier 2 is to
    preserve detail that tier 1 necessarily drops.
    """
    source_texts = fetch_source_texts(
        connection, blob_store, item_id, accepted_formats=LONG_FORM_SUMMARY_FORMATS
    )

    routed = create_model_client("tier_2_summary", settings=settings)
    sections: list[str] = []
    try:
        for source_text in source_texts:
            section = _request_section(routed, source_text)
            if section is None:
                break
            sections.append(section)
    finally:
        routed.client.close()

    if len(sections) != len(source_texts):
        _store_result(
            connection,
            item_id,
            status="failed",
            error="model returned no usable long-form summary output",
        )
        return None

    summary_text = "\n\n".join(sections)
    _store_result(connection, item_id, status="completed", summary_text=summary_text)
    return summary_text
