import json
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from modgud.blobs import BlobStore
from modgud.cli import main
from modgud.database import connect
from modgud.youtube import (
    Caption,
    CaptionRefusal,
    Chapter,
    ExtractedYouTube,
    YoutubeFailure,
)


class _ResponseHandler(BaseHTTPRequestHandler):
    body = b""
    content_type = "application/octet-stream"
    request_count = 0
    status = 200

    def do_GET(self) -> None:
        type(self).request_count += 1
        self.send_response(type(self).status)
        self.send_header("Content-Type", type(self).content_type)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve(
    body: bytes,
    *,
    content_type: str = "application/octet-stream",
    status: int = 200,
) -> Iterator[tuple[str, type[_ResponseHandler]]]:
    class Handler(_ResponseHandler):
        pass

    Handler.body = body
    Handler.content_type = content_type
    Handler.request_count = 0
    Handler.status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/article", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def run_modgud(data_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["modgud", "--data-dir", str(data_dir), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_help_describes_the_command() -> None:
    result = subprocess.run(
        ["modgud", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: modgud" in result.stdout


def test_add_captures_one_item_and_its_raw_content(tmp_path: Path) -> None:
    raw_content = b"<html><article>A useful document</article></html>"
    with serve(raw_content, content_type="text/html; charset=utf-8") as (url, _):
        result = run_modgud(tmp_path, "add", f"{url}/?utm_source=inbox")

    listed = run_modgud(tmp_path, "list")
    stored_blobs = [
        path.read_bytes() for path in (tmp_path / "blobs").rglob("*") if path.is_file()
    ]

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Added item 1: {url}\n"
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.splitlines()[0].split() == [
        "id",
        "format",
        "source",
        "captured-at",
    ]
    assert re.fullmatch(
        r"1\s+web\s+127\.0\.0\.1\s+\d{4}-\d{2}-\d{2}T.*Z",
        listed.stdout.splitlines()[1],
    )
    assert len(listed.stdout.splitlines()) == 2
    assert raw_content in stored_blobs


def test_add_extracts_a_web_post_and_records_its_metadata(tmp_path: Path) -> None:
    raw_content = b"""
        <!doctype html>
        <html>
          <head>
            <meta property="og:title" content="Keeping State Small">
            <meta property="og:site_name" content="Engineering Notes">
            <meta name="author" content="Sam Lee">
          </head>
          <body>
            <nav>Home Products Pricing Sign in</nav>
            <article>
              <h1>Keeping State Small</h1>
              <p>Small state spaces make failures easier to understand because
              every transition has a limited number of possible outcomes.</p>
              <p>Persisting those transitions as events leaves enough evidence
              to explain what happened after the process has restarted.</p>
              <div class="share">Share this article everywhere</div>
            </article>
            <footer>Terms Privacy Careers</footer>
          </body>
        </html>
    """
    with serve(raw_content, content_type="text/html; charset=utf-8") as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            SELECT extracted_text_hash, state, title, author, source
            FROM items
            """
        ).fetchone()
        event_types = [
            row[0] for row in connection.execute("SELECT type FROM events ORDER BY id")
        ]

    assert result.returncode == 0, result.stderr
    assert item is not None
    extracted_text_hash, state, title, author, source = item
    extracted_text = BlobStore(tmp_path / "blobs").get(extracted_text_hash).decode()
    assert (state, title, author, source) == (
        "extracted",
        "Keeping State Small",
        "Sam Lee",
        "Engineering Notes",
    )
    assert "Small state spaces make failures easier to understand" in extracted_text
    assert "Share this article everywhere" not in extracted_text
    assert event_types == ["captured", "extracted"]


def test_add_estimates_web_reading_time_from_extracted_text_not_markup(
    tmp_path: Path,
) -> None:
    article = " ".join(f"article{position}" for position in range(200))
    boilerplate = " ".join(f"navigation{position}" for position in range(1_000))
    raw_content = (
        f"<html><body><nav>{boilerplate}</nav>"
        f"<article><p>{article}</p></article></body></html>"
    ).encode()
    with serve(raw_content, content_type="text/html") as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        estimate = connection.execute(
            "SELECT time_to_value_seconds FROM items"
        ).fetchone()[0]

    assert result.returncode == 0, result.stderr
    assert estimate == 60


def test_add_youtube_stores_metadata_chapters_and_timestamped_captions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "https://www.youtube.com/watch?v=video123"
    captions = b"""WEBVTT

00:00:01.000 --> 00:00:03.500
The first useful claim.

00:01:02.250 --> 00:01:05.000
The supporting evidence.
"""
    chapters: tuple[Chapter, ...] = (
        {"start_time": 0.0, "end_time": 62.0, "title": "The problem"},
        {
            "start_time": 62.0,
            "end_time": 125.75,
            "title": "A durable design",
        },
    )
    extracted = ExtractedYouTube(
        title="How Durable Queues Work",
        channel="Systems Workshop",
        duration_seconds=125.75,
        chapters=chapters,
        caption=Caption(language="en", kind="manual", content=captions),
    )
    monkeypatch.setattr("modgud.cli.extract_youtube", lambda captured_url: extracted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["modgud", "--data-dir", str(tmp_path), "add", url],
    )

    main()

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            SELECT format, state, title, channel, source, duration_seconds,
                   chapters, extracted_text_hash, time_to_value_seconds
            FROM items
            """
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert capsys.readouterr().out == f"Added item 1: {url}\n"
    assert item is not None
    (
        item_format,
        state,
        title,
        channel,
        source,
        duration_seconds,
        stored_chapters,
        transcript_hash,
        time_to_value_seconds,
    ) = item
    assert (
        item_format,
        state,
        title,
        channel,
        source,
        duration_seconds,
        json.loads(stored_chapters),
        time_to_value_seconds,
    ) == (
        "youtube",
        "extracted",
        "How Durable Queues Work",
        "Systems Workshop",
        "Systems Workshop",
        125.75,
        list(chapters),
        126,
    )
    assert BlobStore(tmp_path / "blobs").get(transcript_hash) == captions
    assert [event[0] for event in events] == ["captured", "extracted"]
    extraction = json.loads(events[-1][1])
    assert (extraction["caption_language"], extraction["caption_kind"]) == (
        "en",
        "manual",
    )


def test_add_youtube_records_caption_refusal_as_a_distinct_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "https://www.youtube.com/watch?v=video123"
    extracted = ExtractedYouTube(
        title="How Durable Queues Work",
        channel="Systems Workshop",
        duration_seconds=125.75,
        chapters=(),
        caption_refusal=CaptionRefusal(reason="Sign in to confirm you're not a bot"),
    )
    monkeypatch.setattr("modgud.cli.extract_youtube", lambda captured_url: extracted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["modgud", "--data-dir", str(tmp_path), "add", url],
    )

    main()

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            SELECT state, title, channel, duration_seconds,
                   extracted_text_hash, time_to_value_seconds
            FROM items
            """
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert capsys.readouterr().out == f"Added item 1: {url}\n"
    assert item == (
        "captured",
        "How Durable Queues Work",
        "Systems Workshop",
        125.75,
        None,
        126,
    )
    assert [event[0] for event in events] == ["captured", "caption_refused"]
    refusal = json.loads(events[-1][1])
    assert refusal == {
        "reason": "Sign in to confirm you're not a bot",
        "stage": "captions",
    }


def test_add_youtube_keeps_ordinary_caption_errors_on_the_failed_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = "https://www.youtube.com/watch?v=video123"
    extracted = ExtractedYouTube(
        title="How Durable Queues Work",
        channel="Systems Workshop",
        duration_seconds=125.75,
        chapters=(),
        failure=YoutubeFailure(
            stage="captions",
            reason="Unable to download subtitles: HTTP Error 500",
        ),
    )
    monkeypatch.setattr("modgud.cli.extract_youtube", lambda captured_url: extracted)
    monkeypatch.setattr(
        sys,
        "argv",
        ["modgud", "--data-dir", str(tmp_path), "add", url],
    )

    main()

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT state, duration_seconds, time_to_value_seconds FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert capsys.readouterr().out == f"Added item 1: {url}\n"
    assert item == ("failed", 125.75, 126)
    assert [event[0] for event in events] == ["captured", "failed"]
    failure = json.loads(events[-1][1])
    assert failure == {
        "error": "Unable to download subtitles: HTTP Error 500",
        "stage": "captions",
    }


def test_add_records_web_extraction_failure_without_rejecting_capture(
    tmp_path: Path,
) -> None:
    raw_content = b"<html><head><title>Empty</title></head><body></body></html>"
    with serve(raw_content, content_type="text/html") as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT content_hash, extracted_text_hash, state FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Added item 1: {url}\n"
    assert item is not None
    content_hash, extracted_text_hash, state = item
    assert (extracted_text_hash, state) == (None, "failed")
    assert BlobStore(tmp_path / "blobs").get(content_hash) == raw_content
    assert [event[0] for event in events] == ["captured", "failed"]
    failure = json.loads(events[-1][1])
    assert failure["stage"] == "extraction"
    assert "readable text" in failure["error"]


@pytest.mark.parametrize(
    ("content_type", "raw_content", "expected_format"),
    [
        ("application/octet-stream", b"\x00\x01opaque source material", "unknown"),
        ("application/pdf", b"%PDF-1.7\nsource", "pdf"),
        (
            "application/vnd.ms-powerpoint",
            b"legacy presentation bytes",
            "deck",
        ),
    ],
)
def test_add_accepts_unsupported_formats_as_unsummarizable(
    tmp_path: Path,
    content_type: str,
    raw_content: bytes,
    expected_format: str,
) -> None:
    with serve(raw_content, content_type=content_type) as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT content_hash, format, state FROM items"
        ).fetchone()

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Added item 1: {url}\n"
    assert item is not None
    content_hash, item_format, state = item
    assert (item_format, state) == (expected_format, "unsummarizable")
    assert BlobStore(tmp_path / "blobs").get(content_hash) == raw_content


@pytest.mark.parametrize("submitted", ["not a URL", "http://[invalid"])
def test_add_preserves_malformed_input_instead_of_rejecting_it(
    tmp_path: Path,
    submitted: str,
) -> None:
    result = run_modgud(tmp_path, "add", submitted)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT canonical_url, content_hash, format, state FROM items"
        ).fetchone()
        event_payload = connection.execute("SELECT payload FROM events").fetchone()[0]

    assert result.returncode == 0, result.stderr
    assert item is not None
    canonical_url, content_hash, item_format, state = item
    assert (canonical_url, item_format, state) == (submitted, "unknown", "failed")
    assert BlobStore(tmp_path / "blobs").get(content_hash) == submitted.encode()
    assert json.loads(event_payload)["fetch_error"]


def test_add_preserves_the_input_when_fetching_fails(tmp_path: Path) -> None:
    with serve(b"temporarily unavailable", status=503) as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT content_hash, format, state FROM items"
        ).fetchone()
        event_payload = connection.execute("SELECT payload FROM events").fetchone()[0]

    assert result.returncode == 0, result.stderr
    assert item is not None
    content_hash, item_format, state = item
    assert (item_format, state) == ("unknown", "failed")
    assert BlobStore(tmp_path / "blobs").get(content_hash) == url.encode()
    assert "HTTP Error 503" in json.loads(event_payload)["fetch_error"]


def test_readding_a_known_url_records_a_capture_without_fetching_again(
    tmp_path: Path,
) -> None:
    with serve(b"same response", content_type="text/html") as (url, handler):
        first = run_modgud(tmp_path, "add", url)
        repeated = run_modgud(tmp_path, "add", url)

    listed = run_modgud(tmp_path, "list")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        counts = (
            connection.execute("SELECT count(*) FROM items").fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM events WHERE type = 'captured'"
            ).fetchone()[0],
        )

    assert first.stdout == f"Added item 1: {url}\n"
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout == f"Existing item 1: {url}\n"
    assert handler.request_count == 1
    assert len(listed.stdout.splitlines()) == 2
    assert counts == (1, 2)


def test_matching_content_from_two_urls_resolves_to_the_existing_item(
    tmp_path: Path,
) -> None:
    raw_content = b"identical document bytes"
    with serve(raw_content, content_type="application/pdf") as (url, handler):
        first = run_modgud(tmp_path, "add", url)
        duplicate = run_modgud(tmp_path, "add", f"{url}/mirror")

    listed = run_modgud(tmp_path, "list")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        counts = (
            connection.execute("SELECT count(*) FROM items").fetchone()[0],
            connection.execute("SELECT count(*) FROM events").fetchone()[0],
        )
    stored_blobs = [
        path.read_bytes() for path in (tmp_path / "blobs").rglob("*") if path.is_file()
    ]

    assert first.stdout == f"Added item 1: {url}\n"
    assert duplicate.returncode == 0, duplicate.stderr
    assert duplicate.stdout == f"Existing item 1: {url}\n"
    assert handler.request_count == 2
    assert len(listed.stdout.splitlines()) == 2
    assert counts == (1, 2)
    assert stored_blobs == [raw_content]
