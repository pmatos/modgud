"""Command-line interface for modgud."""

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from modgud.audio_fallbacks import run_audio_fallback_batch
from modgud.blobs import BlobStore
from modgud.capture import CaptureResult, capture_url
from modgud.config import ConfigError, Settings, default_config_path, get_settings
from modgud.database import connect
from modgud.delivery import PostmarkEmailClient, deliver_digest
from modgud.inbound import PostmarkClient, pending_inbound_captures, poll_inbound
from modgud.origin_reports import render_origin_report
from modgud.podcast_transcripts import run_podcast_transcript_batch
from modgud.reprocess import ReprocessError, reprocess_item
from modgud.span_maps import generate_span_map
from modgud.summaries import summarize_item
from modgud.whisper_cpp import WhisperCppError, launch_server


def _default_data_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home is not None:
        return Path(data_home) / "modgud"
    return Path.home() / ".local" / "share" / "modgud"


def _print_capture_result(result: CaptureResult) -> None:
    disposition = "Added" if result.created else "Existing"
    print(f"{disposition} item {result.item_id}: {result.canonical_url}")


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


def _origin_report(data_dir: Path) -> None:
    with connect(data_dir / "modgud.sqlite3") as connection:
        report = render_origin_report(connection)
    print(report)


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


def _reprocess(data_dir: Path, item_id: int) -> None:
    with connect(data_dir / "modgud.sqlite3") as connection:
        state = reprocess_item(connection, BlobStore(data_dir / "blobs"), item_id)
    print(f"Reprocessed item {item_id}: {state}")


def _span_map(data_dir: Path, item_id: int, settings: Settings) -> None:
    with connect(data_dir / "modgud.sqlite3") as connection:
        span_map = generate_span_map(
            connection,
            BlobStore(data_dir / "blobs"),
            item_id,
            settings=settings,
        )
    if span_map is None:
        print(f"Failed to generate span map for item {item_id}")
    else:
        print(f"Generated span map for item {item_id}")


def main(*, local_now: datetime | None = None) -> None:
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
    subparsers.add_parser(
        "origin-report",
        help="report label outcomes by capture origin",
    )
    summarize_parser = subparsers.add_parser(
        "summarize",
        help="generate or replace an item's tier-1 summary",
    )
    summarize_parser.add_argument("item_id", type=int)
    reprocess_parser = subparsers.add_parser(
        "reprocess",
        help="re-run text extraction for a web or PDF item from its stored content",
    )
    reprocess_parser.add_argument("item_id", type=int)
    span_map_parser = subparsers.add_parser(
        "span-map",
        help="generate or replace an audio/video item's span map",
    )
    span_map_parser.add_argument("item_id", type=int)
    poll_inbound_parser = subparsers.add_parser(
        "poll-inbound",
        help="retrieve new inbound messages from Postmark",
    )
    poll_inbound_parser.add_argument(
        "--force",
        action="store_true",
        help="poll now even when the configured interval has not elapsed",
    )
    digest_parser = subparsers.add_parser(
        "digest",
        help="send the morning digest through Postmark",
    )
    digest_parser.add_argument(
        "--now",
        action="store_true",
        help="send immediately instead of waiting for the configured time",
    )
    subparsers.add_parser(
        "whisper-server",
        help="run the configured local transcription endpoint",
    )
    subparsers.add_parser(
        "batch",
        help="run scheduled audio/video processing",
    )
    subparsers.add_parser(
        "serve",
        help="run the LAN web application",
    )

    arguments = parser.parse_args()
    try:
        settings = get_settings(arguments.config)
    except ConfigError as error:
        parser.error(str(error))
    postmark_server_token = settings.secrets.postmark_server_token
    label_token_secret = settings.secrets.label_token_secret
    if arguments.command == "poll-inbound" and postmark_server_token is None:
        parser.error(
            "POSTMARK_SERVER_TOKEN is required to poll Postmark inbound messages"
        )
    if arguments.command == "digest" and postmark_server_token is None:
        parser.error("POSTMARK_SERVER_TOKEN is required to send the digest")
    if arguments.command in {"digest", "serve"} and label_token_secret is None:
        parser.error("LABEL_TOKEN_SECRET is required to sign label links")
    if arguments.command == "digest" and not arguments.now:
        if local_now is None:
            local_now = datetime.now().astimezone()
        current_time = local_now.timetz().replace(tzinfo=None)
        if current_time < settings.digest_send_time:
            configured_time = settings.digest_send_time.strftime("%H:%M")
            print(f"Digest is not due until {configured_time}")
            return
    if arguments.command == "whisper-server":
        try:
            launch_server(settings)
        except WhisperCppError as error:
            parser.error(str(error))
    data_dir: Path = arguments.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if arguments.command == "serve":
        from modgud.web import serve as serve_web_app

        serve_web_app(settings, data_dir)
    elif arguments.command == "add":
        capture_result = capture_url(data_dir, arguments.url, settings)
        if capture_result is None:
            raise RuntimeError("A manual capture must produce a result")
        _print_capture_result(capture_result)
    elif arguments.command == "list":
        _list(data_dir)
    elif arguments.command == "origin-report":
        _origin_report(data_dir)
    elif arguments.command == "summarize":
        _summarize(data_dir, arguments.item_id, settings)
    elif arguments.command == "reprocess":
        try:
            _reprocess(data_dir, arguments.item_id)
        except ReprocessError as error:
            parser.error(str(error))
    elif arguments.command == "span-map":
        _span_map(data_dir, arguments.item_id, settings)
    elif arguments.command == "batch":
        blob_store = BlobStore(data_dir / "blobs")
        result = run_audio_fallback_batch(
            data_dir / "modgud.sqlite3",
            blob_store,
            settings=settings,
        )
        print(
            f"Audio fallback batch: {result.attempted} attempted, "
            f"{result.transcribed} transcribed, {result.failed} failed"
        )
        podcast_result = run_podcast_transcript_batch(
            data_dir / "modgud.sqlite3",
            blob_store,
            settings=settings,
        )
        print(
            f"Podcast transcript batch: {podcast_result.attempted} attempted, "
            f"{podcast_result.feed_supplied} feed-supplied, "
            f"{podcast_result.transcribed} transcribed, "
            f"{podcast_result.failed} failed"
        )
    elif arguments.command == "poll-inbound":
        if postmark_server_token is None:
            raise AssertionError("Postmark token was validated before dispatch")
        poll_result = poll_inbound(
            data_dir / "modgud.sqlite3",
            PostmarkClient(postmark_server_token),
            poll_interval=settings.inbound_poll_interval,
            now=datetime.now(UTC),
            force=arguments.force,
        )
        for pending in pending_inbound_captures(data_dir / "modgud.sqlite3"):
            capture_result = capture_url(
                data_dir,
                pending.target_url,
                settings,
                origin=pending.origin,
                inbound_message_id=pending.message_id,
            )
            if capture_result is not None:
                _print_capture_result(capture_result)
        if poll_result.skipped:
            print("Inbound poll is not due yet")
        else:
            print(
                f"Polled Postmark: {poll_result.new_message_count} new inbound messages"
            )
    else:
        if postmark_server_token is None:
            raise AssertionError("Postmark token was validated before dispatch")
        if label_token_secret is None:
            raise AssertionError("Label token secret was validated before dispatch")
        if arguments.now:
            scheduled_for = None
        else:
            if local_now is None:
                raise AssertionError("Scheduled digest time was initialized")
            scheduled_for = local_now.date()
        digest_result = deliver_digest(
            data_dir / "modgud.sqlite3",
            PostmarkEmailClient(postmark_server_token),
            from_address=settings.digest_from_address,
            to_address=settings.digest_to_address,
            label_base_url=settings.web_bind.base_url,
            label_signing_secret=label_token_secret,
            label_token_lifetime=settings.label_token_lifetime,
            now=local_now,
            scheduled_for=scheduled_for,
        )
        if digest_result.sent:
            item_label = "item" if len(digest_result.item_ids) == 1 else "items"
            print(f"Sent digest with {len(digest_result.item_ids)} {item_label}")
        else:
            print("No digest items to send")
