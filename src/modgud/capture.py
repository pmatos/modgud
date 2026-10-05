"""Capture URLs into durable Items."""

import json
import sqlite3
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from modgud.blobs import BlobStore
from modgud.config import Settings, get_settings
from modgud.database import connect
from modgud.events import ItemLog
from modgud.extraction import ExtractionError, NoTextLayerError, extract_document
from modgud.formats import DOCUMENT_FORMATS, ItemFormat, detect_format
from modgud.podcasts import PodcastFeedError, discover_podcast_feed, parse_podcast_feed
from modgud.summaries import summarize_item
from modgud.time_to_value import recompute_time_to_value
from modgud.urls import canonicalize_url
from modgud.youtube import ExtractedYouTube, extract_youtube

_UNSUMMARIZABLE_FORMATS = frozenset({ItemFormat.DECK, ItemFormat.UNKNOWN})


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """The item identity and outcome produced by a capture attempt."""

    item_id: int
    canonical_url: str
    created: bool


def _fetch(url: str) -> tuple[bytes, str | None, str | None]:
    try:
        request = Request(url, headers={"User-Agent": "modgud/0.1"})
        with urlopen(request) as response:
            return response.read(), response.headers.get("Content-Type"), None
    except (HTTPException, OSError, ValueError) as error:
        raw_input = url.encode("utf-8", errors="surrogateescape")
        return raw_input, None, f"{type(error).__name__}: {error}"


def _inbound_was_processed(
    connection: sqlite3.Connection,
    message_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT processed_at
        FROM postmark_inbound_messages
        WHERE message_id = ?
        """,
        (message_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Inbound message is not queued: {message_id}")
    return row[0] is not None


def _mark_inbound_processed(
    connection: sqlite3.Connection,
    *,
    message_id: str,
    item_id: int,
) -> None:
    updated = connection.execute(
        """
        UPDATE postmark_inbound_messages
        SET item_id = ?,
            processed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE message_id = ? AND processed_at IS NULL
        """,
        (item_id, message_id),
    )
    if updated.rowcount != 1:
        raise RuntimeError(f"Inbound message was processed concurrently: {message_id}")


def _youtube_manifest(
    canonical_url: str,
    extracted: ExtractedYouTube,
) -> bytes:
    return json.dumps(
        {
            "canonical_url": canonical_url,
            "channel": extracted.channel,
            "chapters": extracted.chapters,
            "duration_seconds": extracted.duration_seconds,
            "title": extracted.title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def capture_url(
    data_dir: Path,
    url: str,
    settings: Settings | None,
    *,
    origin: str | None = "manual",
    inbound_message_id: str | None = None,
) -> CaptureResult | None:
    try:
        canonical_url = canonicalize_url(url)
    except ValueError:
        canonical_url = url
    database = data_dir / "modgud.sqlite3"
    with connect(database) as connection:
        if inbound_message_id is not None:
            connection.execute("BEGIN IMMEDIATE")
            if _inbound_was_processed(connection, inbound_message_id):
                return None
        existing = connection.execute(
            "SELECT id FROM items WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if existing is not None:
            item_id = int(existing[0])
            ItemLog(connection, item_id).captured(
                url=url,
                canonical_url=canonical_url,
                origin=origin,
                inbound_message_id=inbound_message_id,
            )
            if inbound_message_id is not None:
                _mark_inbound_processed(
                    connection,
                    message_id=inbound_message_id,
                    item_id=item_id,
                )
            result = CaptureResult(item_id, canonical_url, created=False)
            return result

    detected_format = detect_format(canonical_url)
    extracted_youtube = None
    if detected_format is ItemFormat.YOUTUBE:
        extracted_youtube = extract_youtube(canonical_url)
        content = _youtube_manifest(canonical_url, extracted_youtube)
        content_type = None
        fetch_error = None
    else:
        content, content_type, fetch_error = _fetch(url)
    extracted_podcast = None
    fetched_format = detect_format(
        canonical_url,
        content_type=content_type,
        content=content,
    )
    if fetch_error is None:
        try:
            extracted_podcast = parse_podcast_feed(content, feed_url=canonical_url)
        except PodcastFeedError:
            pass
        else:
            canonical_url = extracted_podcast.canonical_url
            content = extracted_podcast.raw_content
    if (
        extracted_podcast is None
        and fetch_error is None
        and fetched_format in {ItemFormat.PODCAST, ItemFormat.WEB}
    ):
        feed_url = discover_podcast_feed(content, page_url=canonical_url)
        if feed_url is not None:
            feed_content, _, feed_error = _fetch(feed_url)
            if feed_error is None:
                try:
                    extracted_podcast = parse_podcast_feed(
                        feed_content,
                        feed_url=feed_url,
                        episode_url=canonical_url,
                    )
                except PodcastFeedError:
                    pass
                else:
                    canonical_url = extracted_podcast.canonical_url
                    content = extracted_podcast.raw_content
    blob_store = BlobStore(data_dir / "blobs")
    content_hash = blob_store.put(content)
    item_format = detect_format(
        canonical_url,
        content_type=content_type,
        content=content,
    )
    extracted_text_hash = None
    title = None
    author = None
    channel = None
    chapters = None
    extracted_site = None
    extraction_error = None
    extraction_error_stage = "extraction"
    unsummarizable_reason = None
    extracted_text = None
    caption_language = None
    caption_kind = None
    duration_seconds = None
    if extracted_youtube is not None:
        title = extracted_youtube.title
        channel = extracted_youtube.channel
        author = extracted_youtube.channel
        duration_seconds = extracted_youtube.duration_seconds
        chapters = json.dumps(
            extracted_youtube.chapters,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if extracted_youtube.caption is not None:
            extracted_text_hash = blob_store.put(extracted_youtube.caption.content)
            caption_language = extracted_youtube.caption.language
            caption_kind = extracted_youtube.caption.kind
        if extracted_youtube.failure is not None:
            extraction_error = extracted_youtube.failure.reason
            extraction_error_stage = extracted_youtube.failure.stage
    elif extracted_podcast is not None:
        title = extracted_podcast.title
        author = extracted_podcast.author
        channel = extracted_podcast.podcast_title
        duration_seconds = extracted_podcast.duration_seconds
    elif fetch_error is None and item_format in DOCUMENT_FORMATS:
        try:
            extracted_document = extract_document(
                content,
                item_format=item_format,
                url=canonical_url,
            )
        except NoTextLayerError as error:
            unsummarizable_reason = f"{type(error).__name__}: {error}"
        except ExtractionError as error:
            extraction_error = f"{type(error).__name__}: {error}"
        else:
            extracted_text = extracted_document.text
            extracted_text_hash = blob_store.put(extracted_text.encode("utf-8"))
            title = extracted_document.title
            author = extracted_document.author
            extracted_site = extracted_document.site

    if fetch_error is not None:
        item_state = "failed"
    elif item_format in _UNSUMMARIZABLE_FORMATS or unsummarizable_reason is not None:
        item_state = "unsummarizable"
    elif extraction_error is not None:
        item_state = "failed"
    elif extracted_text_hash is not None:
        item_state = "extracted"
    else:
        item_state = "captured"
    try:
        source_url = (
            extracted_podcast.feed_url
            if extracted_podcast is not None
            else canonical_url
        )
        source = urlsplit(source_url).hostname or source_url
    except ValueError:
        source = canonical_url
    if extracted_site is not None:
        source = extracted_site
    elif channel is not None:
        source = channel

    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if inbound_message_id is not None and _inbound_was_processed(
            connection, inbound_message_id
        ):
            return None
        existing = connection.execute(
            """
            SELECT id, canonical_url
            FROM items
            WHERE canonical_url = ? OR content_hash = ?
            ORDER BY id
            LIMIT 1
            """,
            (canonical_url, content_hash),
        ).fetchone()
        if existing is not None:
            item_id = int(existing[0])
            existing_url = str(existing[1])
            if extracted_podcast is not None and extracted_podcast.page_url is not None:
                connection.execute(
                    "UPDATE items SET page_url = ? WHERE id = ? AND page_url IS NOT ?",
                    (
                        extracted_podcast.page_url,
                        item_id,
                        extracted_podcast.page_url,
                    ),
                )
            ItemLog(connection, item_id).captured(
                url=url,
                canonical_url=canonical_url,
                origin=origin,
                inbound_message_id=inbound_message_id,
                podcast=extracted_podcast,
            )
            if inbound_message_id is not None:
                _mark_inbound_processed(
                    connection,
                    message_id=inbound_message_id,
                    item_id=item_id,
                )
            result = CaptureResult(item_id, existing_url, created=False)
            return result

        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url,
                content_hash,
                extracted_text_hash,
                format,
                state,
                source,
                title,
                author,
                channel,
                duration_seconds,
                chapters,
                page_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_url,
                content_hash,
                extracted_text_hash,
                item_format,
                item_state,
                source,
                title,
                author,
                channel,
                duration_seconds,
                chapters,
                extracted_podcast.page_url if extracted_podcast is not None else None,
            ),
        )
        inserted_item_id = cursor.lastrowid
        if inserted_item_id is None:
            raise RuntimeError("SQLite did not return an item id")
        log = ItemLog(connection, inserted_item_id)
        log.captured(
            url=url,
            canonical_url=canonical_url,
            origin=origin,
            inbound_message_id=inbound_message_id,
            fetch_error=fetch_error,
            podcast=extracted_podcast,
        )
        if extracted_text_hash is not None:
            log.extracted(
                extracted_text_hash=extracted_text_hash,
                caption_language=caption_language,
                caption_kind=caption_kind,
            )
        elif extraction_error is not None:
            log.failed(error=extraction_error, stage=extraction_error_stage)
        elif unsummarizable_reason is not None:
            log.unsummarizable(unsummarizable_reason)
        if (
            extracted_youtube is not None
            and extracted_youtube.caption_refusal is not None
        ):
            log.caption_refused(extracted_youtube.caption_refusal.reason)
        if (
            extracted_text_hash is not None
            or extracted_youtube is not None
            or extracted_podcast is not None
        ):
            recompute_time_to_value(
                connection,
                item_id=inserted_item_id,
                extracted_text=extracted_text,
            )
        if inbound_message_id is not None:
            _mark_inbound_processed(
                connection,
                message_id=inbound_message_id,
                item_id=inserted_item_id,
            )

    if item_format is ItemFormat.WEB and extracted_text_hash is not None:
        if settings is None:
            settings = get_settings()
        with connect(database) as connection:
            summarize_item(
                connection,
                blob_store,
                inserted_item_id,
                settings=settings,
            )

    result = CaptureResult(inserted_item_id, canonical_url, created=True)
    return result
