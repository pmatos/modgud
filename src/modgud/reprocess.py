"""Re-run text extraction for an item from the raw content it was captured with."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from modgud.blobs import BlobStore
from modgud.extraction import (
    ExtractedPage,
    ExtractionError,
    NoTextLayerError,
    extract_pdf,
    extract_web_page,
)
from modgud.formats import DOCUMENT_FORMATS, ItemFormat
from modgud.item_lifecycle import (
    ItemTransitionConflict,
    mark_extracted,
    mark_failed,
    mark_unsummarizable,
)
from modgud.time_to_value import recompute_time_to_value


class ReprocessError(ValueError):
    """Raised when an item cannot be reprocessed."""


@contextmanager
def _locked_transition(connection: sqlite3.Connection, item_id: int) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except ItemTransitionConflict as conflict:
        raise ReprocessError(
            f"item {item_id} changed while it was being reprocessed"
        ) from conflict


def reprocess_item(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
) -> str:
    """Extract text for an item that has none, using its stored raw content.

    Returns the state the item was left in.

    Items that already have extracted text are refused: the tier-1 and tier-2
    summaries are keyed on the item and derived from that text, so replacing it
    would leave them describing content the item no longer carries.
    """
    item = connection.execute(
        """
        SELECT format, state, canonical_url, content_hash, extracted_text_hash
        FROM items
        WHERE id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        raise ReprocessError(f"item {item_id} does not exist")
    item_format, state, canonical_url, content_hash, extracted_text_hash = item
    if item_format not in DOCUMENT_FORMATS:
        raise ReprocessError(
            f"item {item_id} has format {item_format}; "
            "only web and pdf items can be reprocessed"
        )
    if extracted_text_hash is not None:
        raise ReprocessError(f"item {item_id} already has extracted text")
    # A failed fetch stores the URL itself as the item's content, so extracting
    # from it would replace the real failure with a misleading parse error.
    fetch_failed = connection.execute(
        """
        SELECT 1
        FROM events
        WHERE item_id = ?
          AND type = 'captured'
          AND json_extract(payload, '$.fetch_error') IS NOT NULL
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    if fetch_failed is not None:
        raise ReprocessError(
            f"item {item_id} was never fetched; its stored content is not the document"
        )

    try:
        content = blob_store.get(str(content_hash))
    except (OSError, ValueError) as unreadable:
        raise ReprocessError(
            f"item {item_id}'s stored content cannot be read: {unreadable}"
        ) from unreadable

    # Extract before taking the write lock: parsing a large PDF can outlast
    # SQLite's busy timeout for the web app's writers.
    try:
        page = _extract(item_format, content, url=str(canonical_url))
    except NoTextLayerError as error:
        with _locked_transition(connection, item_id):
            mark_unsummarizable(
                connection,
                item_id,
                reason=_describe(error),
                expected_state=state,
            )
        return "unsummarizable"
    except ExtractionError as error:
        with _locked_transition(connection, item_id):
            mark_failed(
                connection,
                item_id,
                error=_describe(error),
                stage="extraction",
                expected_state=state,
            )
        return "failed"

    text_hash = blob_store.put(page.text.encode("utf-8"))
    with _locked_transition(connection, item_id):
        mark_extracted(
            connection,
            item_id,
            extracted_text_hash=text_hash,
            expected_state=state,
            title=page.title,
            author=page.author,
            item_source=page.site,
        )
    recompute_time_to_value(connection, item_id=item_id, extracted_text=page.text)
    return "extracted"


def _extract(item_format: str, content: bytes, *, url: str) -> ExtractedPage:
    if item_format == ItemFormat.PDF:
        pdf = extract_pdf(content)
        return ExtractedPage(
            text=pdf.text, title=pdf.title, author=pdf.author, site=None
        )
    return extract_web_page(content, url=url)


def _describe(error: ExtractionError) -> str:
    return f"{type(error).__name__}: {error}"
