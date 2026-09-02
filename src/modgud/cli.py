"""Command-line interface for modgud."""

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from modgud.blobs import BlobStore
from modgud.config import ConfigError, Settings, default_config_path, get_settings
from modgud.database import connect
from modgud.extraction import ExtractionError, extract_web_page
from modgud.formats import ItemFormat, detect_format
from modgud.inbound import PostmarkClient, pending_inbound_captures, poll_inbound
from modgud.podcasts import (
    PodcastEpisode,
    PodcastFeedError,
    discover_podcast_feed,
    parse_podcast_feed,
)
from modgud.summaries import summarize_item
from modgud.time_to_value import recompute_time_to_value
from modgud.urls import canonicalize_url
from modgud.youtube import ExtractedYouTube, extract_youtube

_UNSUMMARIZABLE_FORMATS = frozenset(
    {ItemFormat.DECK, ItemFormat.PDF, ItemFormat.UNKNOWN}
)


def _default_data_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home is not None:
        return Path(data_home) / "modgud"
    return Path.home() / ".local" / "share" / "modgud"


def _fetch(url: str) -> tuple[bytes, str | None, str | None]:
    try:
        request = Request(url, headers={"User-Agent": "modgud/0.1"})
        with urlopen(request) as response:
            return response.read(), response.headers.get("Content-Type"), None
    except (HTTPException, OSError, ValueError) as error:
        raw_input = url.encode("utf-8", errors="surrogateescape")
        return raw_input, None, f"{type(error).__name__}: {error}"


def _record_capture(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    url: str,
    canonical_url: str,
    origin: str | None,
    inbound_message_id: str | None = None,
    fetch_error: str | None = None,
    podcast: PodcastEpisode | None = None,
) -> None:
    fields = {
        "canonical_url": canonical_url,
        "origin": origin,
        "url": url,
    }
    if inbound_message_id is not None:
        fields["inbound_message_id"] = inbound_message_id
    if fetch_error is not None:
        fields["fetch_error"] = fetch_error
    if podcast is not None:
        fields["feed_url"] = podcast.feed_url
        fields["guid"] = podcast.guid
    payload = json.dumps(
        fields,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', ?)",
        (item_id, payload),
    )


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


def _record_extraction(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    extracted_text_hash: str,
    caption_language: str | None = None,
    caption_kind: str | None = None,
) -> None:
    fields = {"extracted_text_hash": extracted_text_hash}
    if caption_language is not None:
        fields["caption_language"] = caption_language
    if caption_kind is not None:
        fields["caption_kind"] = caption_kind
    payload = json.dumps(
        fields,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'extracted', ?)",
        (item_id, payload),
    )


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


def _record_extraction_failure(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    error: str,
    stage: str = "extraction",
) -> None:
    payload = json.dumps(
        {"error": error, "stage": stage},
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'failed', ?)",
        (item_id, payload),
    )


def _record_caption_refusal(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    reason: str,
) -> None:
    payload = json.dumps(
        {"reason": reason, "stage": "captions"},
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO events (item_id, type, payload)
        VALUES (?, 'caption_refused', ?)
        """,
        (item_id, payload),
    )


def _add(
    data_dir: Path,
    url: str,
    settings: Settings,
    *,
    origin: str | None = "manual",
    inbound_message_id: str | None = None,
) -> None:
    try:
        canonical_url = canonicalize_url(url)
    except ValueError:
        canonical_url = url
    database = data_dir / "modgud.sqlite3"
    with connect(database) as connection:
        if inbound_message_id is not None:
            connection.execute("BEGIN IMMEDIATE")
            if _inbound_was_processed(connection, inbound_message_id):
                return
        existing = connection.execute(
            "SELECT id FROM items WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if existing is not None:
            item_id = int(existing[0])
            _record_capture(
                connection,
                item_id=item_id,
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
            print(f"Existing item {item_id}: {canonical_url}")
            return

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
    elif fetch_error is None and item_format is ItemFormat.WEB:
        try:
            extracted_page = extract_web_page(content, url=canonical_url)
        except ExtractionError as error:
            extraction_error = f"{type(error).__name__}: {error}"
        else:
            extracted_text = extracted_page.text
            extracted_text_hash = blob_store.put(extracted_text.encode("utf-8"))
            title = extracted_page.title
            author = extracted_page.author
            extracted_site = extracted_page.site

    if fetch_error is not None:
        item_state = "failed"
    elif item_format in _UNSUMMARIZABLE_FORMATS:
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
            return
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
            _record_capture(
                connection,
                item_id=item_id,
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
            print(f"Existing item {item_id}: {existing_url}")
            return

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
                chapters
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        inserted_item_id = cursor.lastrowid
        if inserted_item_id is None:
            raise RuntimeError("SQLite did not return an item id")
        _record_capture(
            connection,
            item_id=inserted_item_id,
            url=url,
            canonical_url=canonical_url,
            origin=origin,
            inbound_message_id=inbound_message_id,
            fetch_error=fetch_error,
            podcast=extracted_podcast,
        )
        if extracted_text_hash is not None:
            _record_extraction(
                connection,
                item_id=inserted_item_id,
                extracted_text_hash=extracted_text_hash,
                caption_language=caption_language,
                caption_kind=caption_kind,
            )
        elif extraction_error is not None:
            _record_extraction_failure(
                connection,
                item_id=inserted_item_id,
                error=extraction_error,
                stage=extraction_error_stage,
            )
        if (
            extracted_youtube is not None
            and extracted_youtube.caption_refusal is not None
        ):
            _record_caption_refusal(
                connection,
                item_id=inserted_item_id,
                reason=extracted_youtube.caption_refusal.reason,
            )
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
        with connect(database) as connection:
            summarize_item(
                connection,
                blob_store,
                inserted_item_id,
                settings=settings,
            )

    print(f"Added item {inserted_item_id}: {canonical_url}")


def _list(data_dir: Path) -> None:
    with connect(data_dir / "modgud.sqlite3") as connection:
        items = connection.execute(
            """
            SELECT items.id, items.format, items.source, max(events.created_at)
            FROM items
            JOIN events ON events.item_id = items.id
            WHERE events.type = 'captured'
            GROUP BY items.id
            ORDER BY items.id
            """
        ).fetchall()

    print(f"{'id':<4} {'format':<10} {'source':<24} captured-at")
    for item_id, item_format, source, captured_at in items:
        print(f"{item_id:<4} {item_format:<10} {source:<24} {captured_at}")


def _summarize(data_dir: Path, item_id: int, settings: Settings) -> None:
    with connect(data_dir / "modgud.sqlite3") as connection:
        summary = summarize_item(
            connection,
            BlobStore(data_dir / "blobs"),
            item_id,
            settings=settings,
        )
    if summary is None:
        print(f"Failed to summarize item {item_id}")
    else:
        print(f"Summarized item {item_id}")


def main() -> None:
    """Run the modgud command-line interface."""
    parser = argparse.ArgumentParser(
        prog="modgud",
        description="Triage personal content.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="path to the operator configuration file",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_default_data_dir(),
        help="directory for the database and raw content",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add", help="capture a URL")
    add_parser.add_argument("url")
    subparsers.add_parser("list", help="list captured items")
    summarize_parser = subparsers.add_parser(
        "summarize",
        help="generate or replace an item's tier-1 summary",
    )
    summarize_parser.add_argument("item_id", type=int)
    poll_inbound_parser = subparsers.add_parser(
        "poll-inbound",
        help="retrieve new inbound messages from Postmark",
    )
    poll_inbound_parser.add_argument(
        "--force",
        action="store_true",
        help="poll now even when the configured interval has not elapsed",
    )

    arguments = parser.parse_args()
    try:
        settings = get_settings(arguments.config)
    except ConfigError as error:
        parser.error(str(error))
    postmark_server_token = settings.secrets.postmark_server_token
    if arguments.command == "poll-inbound" and postmark_server_token is None:
        parser.error(
            "POSTMARK_SERVER_TOKEN is required to poll Postmark inbound messages"
        )
    data_dir: Path = arguments.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if arguments.command == "add":
        _add(data_dir, arguments.url, settings)
    elif arguments.command == "list":
        _list(data_dir)
    elif arguments.command == "summarize":
        _summarize(data_dir, arguments.item_id, settings)
    else:
        if postmark_server_token is None:
            raise AssertionError("Postmark token was validated before dispatch")
        result = poll_inbound(
            data_dir / "modgud.sqlite3",
            PostmarkClient(postmark_server_token),
            poll_interval=settings.inbound_poll_interval,
            now=datetime.now(UTC),
            force=arguments.force,
        )
        for pending in pending_inbound_captures(data_dir / "modgud.sqlite3"):
            _add(
                data_dir,
                pending.target_url,
                settings,
                origin=pending.origin,
                inbound_message_id=pending.message_id,
            )
        if result.skipped:
            print("Inbound poll is not due yet")
        else:
            print(f"Polled Postmark: {result.new_message_count} new inbound messages")
