"""Re-run text extraction for an item from the raw content it was captured with."""

import sqlite3
from dataclasses import dataclass

from modgud.blobs import BlobStore
from modgud.events import ItemLog
from modgud.extraction import (
    ExtractionError,
    NoTextLayerError,
    extract_pdf,
    extract_web_page,
)
from modgud.formats import ItemFormat
from modgud.time_to_value import recompute_time_to_value

_REPROCESSABLE_FORMATS = frozenset({ItemFormat.WEB, ItemFormat.PDF})


class ReprocessError(ValueError):
    """Raised when an item cannot be reprocessed."""


@dataclass(frozen=True, slots=True)
class ReprocessResult:
    """The state an item was left in by a reprocess."""

    item_id: int
    state: str


@dataclass(frozen=True, slots=True)
class _Text:
    text: str
    title: str | None
    author: str | None
    site: str | None


@dataclass(frozen=True, slots=True)
class _NoText:
    reason: str


@dataclass(frozen=True, slots=True)
class _Failure:
    error: str


def reprocess_item(
    connection: sqlite3.Connection,
    blob_store: BlobStore,
    item_id: int,
) -> ReprocessResult:
    """Extract text for an item that has none, using its stored raw content.

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
    if item_format not in _REPROCESSABLE_FORMATS:
        raise ReprocessError(
            f"item {item_id} is a {item_format} item; "
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

    # Extract before taking the write lock: parsing a large PDF can outlast
    # SQLite's busy timeout for the web app's writers.
    outcome = _extract(
        ItemFormat(item_format),
        blob_store.get(str(content_hash)),
        url=str(canonical_url),
    )

    match outcome:
        case _Text(text=text, title=title, author=author, site=site):
            text_hash = blob_store.put(text.encode("utf-8"))
            _apply(
                connection,
                item_id,
                from_state=state,
                to_state="extracted",
                text_hash=text_hash,
                title=title,
                author=author,
                site=site,
            )
            ItemLog(connection, item_id).extracted(extracted_text_hash=text_hash)
            recompute_time_to_value(connection, item_id=item_id, extracted_text=text)
            new_state = "extracted"
        case _NoText(reason=reason):
            _apply(connection, item_id, from_state=state, to_state="unsummarizable")
            ItemLog(connection, item_id).unsummarizable(reason)
            new_state = "unsummarizable"
        case _Failure(error=error):
            _apply(connection, item_id, from_state=state, to_state="failed")
            ItemLog(connection, item_id).failed(error=error, stage="extraction")
            new_state = "failed"
    return ReprocessResult(item_id=item_id, state=new_state)


def _apply(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    from_state: str,
    to_state: str,
    text_hash: str | None = None,
    title: str | None = None,
    author: str | None = None,
    site: str | None = None,
) -> None:
    """Move the item to its new state, unless something else already changed it."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    updated = connection.execute(
        """
        UPDATE items
        SET state = ?,
            extracted_text_hash = ?,
            title = coalesce(?, title),
            author = coalesce(?, author),
            source = coalesce(?, source),
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
          AND state = ?
          AND extracted_text_hash IS NULL
        """,
        (to_state, text_hash, title, author, site, item_id, from_state),
    )
    if updated.rowcount != 1:
        raise ReprocessError(f"item {item_id} changed while it was being reprocessed")


def _extract(
    item_format: ItemFormat, content: bytes, *, url: str
) -> _Text | _NoText | _Failure:
    if item_format is ItemFormat.PDF:
        try:
            pdf = extract_pdf(content)
        except NoTextLayerError as error:
            return _NoText(f"{type(error).__name__}: {error}")
        except ExtractionError as error:
            return _Failure(f"{type(error).__name__}: {error}")
        return _Text(text=pdf.text, title=pdf.title, author=pdf.author, site=None)
    try:
        page = extract_web_page(content, url=url)
    except ExtractionError as error:
        return _Failure(f"{type(error).__name__}: {error}")
    return _Text(text=page.text, title=page.title, author=page.author, site=page.site)
