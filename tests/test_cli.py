import io
import json
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, NamedTuple
from urllib.parse import urlsplit

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from modgud.blobs import BlobStore
from modgud.cli import main
from modgud.database import connect
from modgud.delivery import DigestEmail, PostmarkEmailClient
from modgud.events import ItemLog
from modgud.reprocess import ReprocessError, reprocess_item
from modgud.youtube import (
    Caption,
    CaptionRefusal,
    Chapter,
    ExtractedYouTube,
    YoutubeFailure,
)


@pytest.fixture(autouse=True)
def operator_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    class SummaryHandler(_SummaryResponseHandler):
        pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SummaryHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    config_home = tmp_path / "config-home"
    config_dir = config_home / "modgud"
    config_dir.mkdir(parents=True)
    example = Path(__file__).parents[1] / "config.example.toml"
    (config_dir / "config.toml").write_text(
        example.read_text(encoding="utf-8").replace(
            'base_url = "http://127.0.0.1:11434/v1"',
            f'base_url = "http://127.0.0.1:{server.server_address[1]}/v1"',
            1,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv(
        "LABEL_TOKEN_SECRET", "a-dedicated-test-secret-with-at-least-32-bytes"
    )
    try:
        yield
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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


class _RouteResponseHandler(_ResponseHandler):
    request_counts: ClassVar[dict[str, int]] = {}
    routes: ClassVar[dict[str, tuple[bytes, str]]] = {}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        type(self).request_counts[path] = type(self).request_counts.get(path, 0) + 1
        response = type(self).routes.get(path)
        if response is None:
            self.send_error(404)
            return
        body, content_type = response
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)


class _SummaryResponseHandler(_ResponseHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(content_length))
        response = json.dumps(
            {
                "id": "test-summary",
                "object": "chat.completion",
                "created": 0,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "one_liner": "A compact description of the article.",
                                    "claims": [
                                        "The article makes its first claim.",
                                        "The article makes its second claim.",
                                        "The article makes its third claim.",
                                    ],
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


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


@contextmanager
def serve_routes(
    routes: dict[str, tuple[bytes, str]],
) -> Iterator[tuple[str, type[_RouteResponseHandler]]]:
    class Handler(_RouteResponseHandler):
        pass

    Handler.request_counts = {}
    Handler.routes = routes
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}", Handler
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


def _pdf_bytes(
    text: str | None,
    *,
    title: str | None = None,
    author: str | None = None,
) -> bytes:
    """Build a minimal one-page PDF, optionally with a text layer and metadata."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)

    if text is not None:
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 24 Tf 20 250 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)

        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

    metadata = {}
    if title is not None:
        metadata["/Title"] = title
    if author is not None:
        metadata["/Author"] = author
    if metadata:
        writer.metadata = metadata

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_help_describes_the_command() -> None:
    result = subprocess.run(
        ["modgud", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: modgud" in result.stdout


def test_origin_report_leads_with_recorded_origin_coverage(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        for position, origin in enumerate(("briefing@example.com", "manual", None)):
            item = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, format, state, source
                ) VALUES (?, ?, 'web', 'summarized', 'example.com')
                """,
                (f"https://example.com/{position}", f"content-{position}"),
            )
            payload = json.dumps({"origin": origin}) if origin is not None else "{}"
            connection.execute(
                "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', ?)",
                (item.lastrowid, payload),
            )

    result = run_modgud(tmp_path, "origin-report")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "Origin coverage: 2/3 items (66.7%)"


def test_origin_report_groups_label_outcomes_and_withholds_thin_rankings(
    tmp_path: Path,
) -> None:
    positions = iter(range(1, 11))

    def add_item(origin: str | None, *labels: str) -> None:
        position = next(positions)
        with connect(tmp_path / "modgud.sqlite3") as connection:
            item = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, format, state, source
                ) VALUES (?, ?, 'web', 'summarized', 'example.com')
                """,
                (f"https://example.com/{position}", f"content-{position}"),
            )
            connection.execute(
                "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', ?)",
                (item.lastrowid, json.dumps({"origin": origin})),
            )
            for label in labels:
                connection.execute(
                    "INSERT INTO events (item_id, type, payload) VALUES (?, 'label', ?)",
                    (item.lastrowid, json.dumps({"label": label})),
                )

    add_item("briefing@example.com", "worth-it", "not-worth-it")
    for _ in range(3):
        add_item("briefing@example.com", "not-worth-it")
    add_item("briefing@example.com", "worth-it")
    add_item("roundup@example.com", "worth-it")
    add_item("roundup@example.com", "not-worth-it")
    add_item("roundup@example.com")
    add_item("manual")
    add_item(None)

    result = run_modgud(tmp_path, "origin-report")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "Origin coverage: 9/10 items (90.0%)"
    assert lines[1] == "Manual captures: 1/10 items (10.0%)"
    assert re.fullmatch(
        r"1\s+briefing@example\.com\s+5\s+1\s+4\s+0\s+80\.0% not worth it",
        next(line for line in lines if "briefing@example.com" in line),
    )
    assert re.fullmatch(
        r"-\s+roundup@example\.com\s+3\s+1\s+1\s+1\s+"
        r"Too little data \(2/5 labelled\)",
        next(line for line in lines if "roundup@example.com" in line),
    )
    assert "Manual capture (not ranked)" in next(
        line for line in lines if re.search(r"\smanual\s", line)
    )
    assert "Origin not recorded" in next(line for line in lines if "(unknown)" in line)


def test_origin_report_is_exportable_before_any_items_are_captured(
    tmp_path: Path,
) -> None:
    result = run_modgud(tmp_path, "origin-report")

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Origin coverage: 0/0 items (0.0%)\n"
        "Manual captures: 0/0 items (0.0%)\n"
        "\n"
        "Rank  Origin  Items  Worth it  Not worth it  Unlabelled  Result\n"
    )


def test_whisper_server_uses_the_configured_route_model_and_threads(
    tmp_path: Path,
) -> None:
    whisper_root = tmp_path / "whisper.cpp"
    server = whisper_root / "build/bin/whisper-server"
    server.parent.mkdir(parents=True)
    server.write_text(
        "#!/usr/bin/python3\n"
        "import json\n"
        "import sys\n"
        "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    server.chmod(0o755)
    model = whisper_root / "models/ggml-tiny.en.bin"
    model.parent.mkdir()
    model.write_bytes(b"known test model")
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8")
        .replace(
            'root = "~/.local/share/modgud/whisper.cpp"',
            f'root = "{whisper_root}"',
        )
        .replace('model_size = "large-v3-turbo"', 'model_size = "tiny.en"')
        .replace("threads = 12", "threads = 7"),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"

    result = run_modgud(
        data_dir,
        "--config",
        str(config_path),
        "whisper-server",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "--host",
        "127.0.0.1",
        "--port",
        "8080",
        "--model",
        str(model),
        "--threads",
        "7",
        "--language",
        "auto",
        "--inference-path",
        "/v1/audio/transcriptions",
        "--convert",
    ]
    assert not data_dir.exists()


def test_whisper_server_names_a_model_that_has_not_been_downloaded(
    tmp_path: Path,
) -> None:
    whisper_root = tmp_path / "whisper.cpp"
    server = whisper_root / "build/bin/whisper-server"
    server.parent.mkdir(parents=True)
    server.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    server.chmod(0o755)
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8").replace(
            'root = "~/.local/share/modgud/whisper.cpp"',
            f'root = "{whisper_root}"',
        ),
        encoding="utf-8",
    )
    expected_model = whisper_root / "models/ggml-large-v3-turbo.bin"

    result = run_modgud(
        tmp_path / "data",
        "--config",
        str(config_path),
        "whisper-server",
    )

    assert result.returncode == 2
    assert f"whisper.cpp model not found: {expected_model}" in result.stderr
    assert "Traceback" not in result.stderr


def test_startup_stops_before_creating_data_when_config_is_missing(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    missing_config = tmp_path / "missing.toml"

    result = subprocess.run(
        [
            "modgud",
            "--config",
            str(missing_config),
            "--data-dir",
            str(data_dir),
            "list",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert f"Configuration file not found: {missing_config}" in result.stderr
    assert not data_dir.exists()


def test_runtime_secret_is_neither_printed_nor_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postmark-secret-that-must-stay-in-memory"
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", secret)

    result = run_modgud(tmp_path, "list")
    persisted = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )

    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert secret.encode() not in persisted


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
        "summarized",
        "Keeping State Small",
        "Sam Lee",
        "Engineering Notes",
    )
    assert "Small state spaces make failures easier to understand" in extracted_text
    assert "Share this article everywhere" not in extracted_text
    assert event_types == ["captured", "extracted", "summarized"]


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


def test_add_feed_captures_only_the_latest_podcast_episode_and_metadata(
    tmp_path: Path,
) -> None:
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"
             xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
          <channel>
            <title>Systems from First Principles</title>
            <item>
              <guid isPermaLink="false">older-episode-guid</guid>
              <title>Old News</title>
              <author>archive@example.com (Archive Host)</author>
              <pubDate>Tue, 01 Sep 2026 09:00:00 GMT</pubDate>
              <itunes:duration>15:00</itunes:duration>
            </item>
            <item>
              <guid isPermaLink="false">latest-episode-guid</guid>
              <title>Queues Are Coordination</title>
              <itunes:author>Mina Cho</itunes:author>
              <pubDate>Wed, 02 Sep 2026 09:00:00 GMT</pubDate>
              <itunes:duration>1:02:03</itunes:duration>
            </item>
          </channel>
        </rss>
    """
    with serve(feed, content_type="application/rss+xml; charset=utf-8") as (
        feed_url,
        handler,
    ):
        result = run_modgud(tmp_path, "add", feed_url)

    listed = run_modgud(tmp_path, "list")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        items = connection.execute(
            """
            SELECT canonical_url, format, state, title, author, channel, source,
                   duration_seconds, time_to_value_seconds
            FROM items
            """
        ).fetchall()

    assert result.returncode == 0, result.stderr
    assert handler.request_count == 1
    assert len(items) == 1
    canonical_url, *metadata = items[0]
    assert re.fullmatch(
        r"podcast:[0-9a-f]{64}/latest-episode-guid",
        canonical_url,
    )
    assert metadata == [
        "podcast",
        "captured",
        "Queues Are Coordination",
        "Mina Cho",
        "Systems from First Principles",
        "Systems from First Principles",
        3723.0,
        3723,
    ]
    assert result.stdout == f"Added item 1: {canonical_url}\n"
    assert len(listed.stdout.splitlines()) == 2
    assert re.fullmatch(
        r"1\s+podcast\s+Systems from First Principles\s+\d{4}-\d{2}-\d{2}T.*Z",
        listed.stdout.splitlines()[1],
    )


def test_episode_page_and_its_feed_resolve_to_the_same_episode(tmp_path: Path) -> None:
    page = b"""
        <html>
          <head>
            <link rel="alternate" type="application/rss+xml" href="/feed.xml">
          </head>
          <body><h1>Queues Are Coordination</h1></body>
        </html>
    """
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"
             xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
          <channel>
            <title>Systems from First Principles</title>
            <item>
              <guid isPermaLink="false">episode-page-guid</guid>
              <link>/episodes/queues</link>
              <title>Queues Are Coordination</title>
              <enclosure url="/audio/queues.mp3" type="audio/mpeg" />
              <itunes:duration>42:10</itunes:duration>
            </item>
          </channel>
        </rss>
    """
    routes = {
        "/episodes/queues": (page, "text/html; charset=utf-8"),
        "/feed.xml": (feed, "application/rss+xml"),
    }
    with serve_routes(routes) as (server_url, handler):
        episode_url = f"{server_url}/episodes/queues?utm_source=inbox"
        feed_url = f"{server_url}/feed.xml"
        from_episode = run_modgud(tmp_path, "add", episode_url)
        from_feed = run_modgud(tmp_path, "add", feed_url)

    listed = run_modgud(tmp_path, "list")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_count = connection.execute("SELECT count(*) FROM items").fetchone()[0]
        capture_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'captured'"
        ).fetchone()[0]
        capture_payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM events WHERE type = 'captured' ORDER BY id"
            )
        ]
        canonical_url, page_url = connection.execute(
            "SELECT canonical_url, page_url FROM items"
        ).fetchone()

    assert page_url == f"{server_url}/episodes/queues"
    assert from_episode.returncode == 0, from_episode.stderr
    assert from_feed.returncode == 0, from_feed.stderr
    assert from_episode.stdout == f"Added item 1: {canonical_url}\n"
    assert from_feed.stdout == f"Existing item 1: {canonical_url}\n"
    assert re.fullmatch(r"podcast:[0-9a-f]{64}/episode-page-guid", canonical_url)
    assert handler.request_counts == {"/episodes/queues": 1, "/feed.xml": 2}
    assert (item_count, capture_count) == (1, 2)
    assert capture_payloads == [
        {
            "canonical_url": canonical_url,
            "feed_url": feed_url,
            "guid": "episode-page-guid",
            "origin": "manual",
            "url": episode_url,
        },
        {
            "canonical_url": canonical_url,
            "feed_url": feed_url,
            "guid": "episode-page-guid",
            "origin": "manual",
            "url": feed_url,
        },
    ]
    assert len(listed.stdout.splitlines()) == 2


def _episode_feed(*, link: str | None) -> bytes:
    link_element = f"<link>{link}</link>" if link is not None else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Systems from First Principles</title>
            <item>
              <guid isPermaLink="false">episode-guid</guid>
              {link_element}
              <title>Queues Are Coordination</title>
              <enclosure url="/audio/queues.mp3" type="audio/mpeg" />
            </item>
          </channel>
        </rss>
    """.encode()


def test_recapturing_a_feed_that_gained_a_link_backfills_the_page_url(
    tmp_path: Path,
) -> None:
    routes = {"/feed.xml": (_episode_feed(link=None), "application/rss+xml")}
    with serve_routes(routes) as (server_url, handler):
        feed_url = f"{server_url}/feed.xml"
        first = run_modgud(tmp_path, "add", feed_url)
        with connect(tmp_path / "modgud.sqlite3") as connection:
            page_url_after_first_capture = connection.execute(
                "SELECT page_url FROM items"
            ).fetchone()[0]

        handler.routes["/feed.xml"] = (
            _episode_feed(link="/episodes/queues"),
            "application/rss+xml",
        )
        second = run_modgud(tmp_path, "add", feed_url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_count, page_url_after_second_capture = connection.execute(
            "SELECT count(*), page_url FROM items"
        ).fetchone()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert page_url_after_first_capture is None
    assert item_count == 1
    assert page_url_after_second_capture == f"{server_url}/episodes/queues"


def test_blog_post_listed_in_its_sites_feed_is_captured_as_a_web_page(
    tmp_path: Path,
) -> None:
    page = b"""
        <html>
          <head>
            <title>Shipping a Software Factory</title>
            <link rel="alternate" type="application/rss+xml" href="/feed.xml">
          </head>
          <body>
            <article>
              <h1>Shipping a Software Factory</h1>
              <p>We merged a thousand pull requests in a single week by letting
              agents own the whole loop from ticket to merge, and this post
              explains how the pipeline, the review gates, and the rollback
              policy were designed to keep that pace safe.</p>
              <p>The first lesson was that small, auditable changes matter more
              than raw throughput, because every merged change has to remain
              understandable to the humans who supervise the system.</p>
            </article>
          </body>
        </html>
    """
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Engineering Blog</title>
            <item>
              <guid isPermaLink="true">/blog/software-factory</guid>
              <link>/blog/software-factory</link>
              <title>Shipping a Software Factory</title>
            </item>
          </channel>
        </rss>
    """
    routes = {
        "/blog/software-factory": (page, "text/html; charset=utf-8"),
        "/feed.xml": (feed, "application/rss+xml"),
    }
    with serve_routes(routes) as (server_url, _):
        post_url = f"{server_url}/blog/software-factory"
        result = run_modgud(tmp_path, "add", post_url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        items = connection.execute(
            "SELECT canonical_url, format, title FROM items"
        ).fetchall()

    assert result.returncode == 0, result.stderr
    assert items == [(post_url, "web", "Shipping a Software Factory")]


def test_the_same_guid_in_two_feeds_has_two_feed_scoped_identities(
    tmp_path: Path,
) -> None:
    feed = b"""<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <title>A Syndicated Show</title>
            <item>
              <guid>shared-guid</guid>
              <title>A Shared Episode</title>
            </item>
          </channel>
        </rss>
    """
    routes = {
        "/primary.xml": (feed, "application/rss+xml"),
        "/mirror.xml": (feed, "application/rss+xml"),
    }
    with serve_routes(routes) as (server_url, _):
        primary = run_modgud(tmp_path, "add", f"{server_url}/primary.xml")
        mirror = run_modgud(tmp_path, "add", f"{server_url}/mirror.xml")

    with connect(tmp_path / "modgud.sqlite3") as connection:
        canonical_urls = [
            row[0]
            for row in connection.execute("SELECT canonical_url FROM items ORDER BY id")
        ]

    assert primary.returncode == 0, primary.stderr
    assert mirror.returncode == 0, mirror.stderr
    assert len(canonical_urls) == 2
    assert canonical_urls[0] != canonical_urls[1]
    assert all(
        re.fullmatch(r"podcast:[0-9a-f]{64}/shared-guid", canonical_url)
        for canonical_url in canonical_urls
    )


def test_add_atom_feed_captures_its_latest_podcast_entry(tmp_path: Path) -> None:
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
          <title>Practical Reliability</title>
          <entry>
            <id>atom-latest-guid</id>
            <title>Recovery Is a Product Feature</title>
            <author><name>Ada Rivera</name></author>
            <published>2026-09-02T11:30:00Z</published>
            <link rel="alternate" href="/episodes/recovery" />
            <itunes:duration>95.5</itunes:duration>
          </entry>
          <entry>
            <id>atom-older-guid</id>
            <title>An Older Entry</title>
            <published>2026-09-01T11:30:00Z</published>
          </entry>
        </feed>
    """
    with serve(feed, content_type="application/atom+xml") as (feed_url, _):
        result = run_modgud(tmp_path, "add", feed_url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            SELECT canonical_url, format, title, author, channel,
                   duration_seconds, time_to_value_seconds
            FROM items
            """
        ).fetchone()

    assert result.returncode == 0, result.stderr
    assert item is not None
    canonical_url, *metadata = item
    assert re.fullmatch(r"podcast:[0-9a-f]{64}/atom-latest-guid", canonical_url)
    assert metadata == [
        "podcast",
        "Recovery Is a Product Feature",
        "Ada Rivera",
        "Practical Reliability",
        95.5,
        96,
    ]


def test_feed_is_parsed_when_the_server_uses_a_generic_xml_content_type(
    tmp_path: Path,
) -> None:
    feed = b"""<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <title>The Misconfigured Feed</title>
            <item>
              <guid>generic-content-type-guid</guid>
              <title>Still a Podcast Episode</title>
            </item>
          </channel>
        </rss>
    """
    with serve(feed, content_type="text/xml") as (feed_url, _):
        result = run_modgud(tmp_path, "add", feed_url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT canonical_url, format, title, source FROM items"
        ).fetchone()

    assert result.returncode == 0, result.stderr
    assert item is not None
    canonical_url, item_format, title, source = item
    assert re.fullmatch(
        r"podcast:[0-9a-f]{64}/generic-content-type-guid",
        canonical_url,
    )
    assert (item_format, title, source) == (
        "podcast",
        "Still a Podcast Episode",
        "The Misconfigured Feed",
    )


def test_invalid_podcast_duration_does_not_reject_the_episode(tmp_path: Path) -> None:
    feed = b"""<?xml version="1.0"?>
        <rss version="2.0"
             xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
          <channel>
            <title>Imperfect but Useful</title>
            <item>
              <guid>bad-duration-guid</guid>
              <title>An Episode with Bad Duration Metadata</title>
              <itunes:duration>-5</itunes:duration>
            </item>
          </channel>
        </rss>
    """
    with serve(feed, content_type="application/rss+xml") as (feed_url, _):
        result = run_modgud(tmp_path, "add", feed_url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT state, duration_seconds, time_to_value_seconds FROM items"
        ).fetchone()

    assert result.returncode == 0, result.stderr
    assert item == ("captured", None, None)


def test_digest_now_sends_the_current_selection_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title
            ) VALUES (
                'https://example.com/digest', 'digest-content', 'web',
                'failed', 'example.com', 'Digest item'
            )
            """
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
            (item.lastrowid,),
        )
    sent_messages: list[DigestEmail] = []

    def record_send(
        _client: PostmarkEmailClient,
        message: DigestEmail,
    ) -> str:
        sent_messages.append(message)
        return "postmark-message-1"

    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token-secret")
    monkeypatch.setattr(PostmarkEmailClient, "send_email", record_send)
    monkeypatch.setattr(
        sys,
        "argv",
        ["modgud", "--data-dir", str(tmp_path), "digest", "--now"],
    )

    main()

    assert capsys.readouterr().out == "Sent digest with 1 item\n"
    assert len(sent_messages) == 1
    assert sent_messages[0].to_address == "reader@example.com"


def test_digest_fails_loudly_when_the_label_signing_secret_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token-secret")
    monkeypatch.delenv("LABEL_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["modgud", "--data-dir", str(tmp_path), "digest", "--now"],
    )

    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 2
    assert (
        "LABEL_TOKEN_SECRET is required to sign label links" in capsys.readouterr().err
    )


def test_scheduled_digest_does_nothing_before_the_configured_local_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source
            ) VALUES (
                'https://example.com/later', 'later-content', 'web',
                'failed', 'example.com'
            )
            """
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
            (item.lastrowid,),
        )

    def reject_send(
        _client: PostmarkEmailClient,
        _message: DigestEmail,
    ) -> str:
        raise AssertionError("email must not be sent before the configured time")

    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token-secret")
    monkeypatch.setattr(PostmarkEmailClient, "send_email", reject_send)
    monkeypatch.setattr(
        sys,
        "argv",
        ["modgud", "--data-dir", str(tmp_path), "digest"],
    )

    main(
        local_now=datetime(
            2026,
            9,
            3,
            6,
            59,
            tzinfo=timezone(timedelta(hours=2)),
        )
    )

    with connect(tmp_path / "modgud.sqlite3") as connection:
        sent_event_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'digest_sent'"
        ).fetchone()[0]
    assert capsys.readouterr().out == "Digest is not due until 07:00\n"
    assert sent_event_count == 0


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


def test_add_extracts_a_pdf_and_summarize_produces_a_tier_1_artifact(
    tmp_path: Path,
) -> None:
    raw_content = _pdf_bytes(
        "Smaller deployments reduce recovery time and limit operational risk.",
        title="A Study of Safe Deployments",
        author="Ada Rivera",
    )
    with serve(raw_content, content_type="application/pdf") as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT extracted_text_hash, state, title, author, format FROM items"
        ).fetchone()
        event_types = [
            row[0] for row in connection.execute("SELECT type FROM events ORDER BY id")
        ]

    assert result.returncode == 0, result.stderr
    assert item is not None
    extracted_text_hash, state, title, author, item_format = item
    extracted_text = BlobStore(tmp_path / "blobs").get(extracted_text_hash).decode()
    assert (state, title, author, item_format) == (
        "extracted",
        "A Study of Safe Deployments",
        "Ada Rivera",
        "pdf",
    )
    assert "Smaller deployments reduce recovery time" in extracted_text
    assert event_types == ["captured", "extracted"]

    summarize_result = run_modgud(tmp_path, "summarize", "1")

    with connect(tmp_path / "modgud.sqlite3") as connection:
        state_after_summarize = connection.execute(
            "SELECT state FROM items WHERE id = 1"
        ).fetchone()[0]
        summary = connection.execute(
            "SELECT one_liner, claims FROM tier_1_summaries WHERE item_id = 1"
        ).fetchone()

    assert summarize_result.returncode == 0, summarize_result.stderr
    assert summarize_result.stdout == "Summarized item 1\n"
    assert state_after_summarize == "summarized"
    assert summary is not None


def test_add_records_a_textless_pdf_as_unsummarizable_with_a_reason(
    tmp_path: Path,
) -> None:
    raw_content = _pdf_bytes(None)
    with serve(raw_content, content_type="application/pdf") as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT content_hash, extracted_text_hash, state FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert result.returncode == 0, result.stderr
    assert item is not None
    content_hash, extracted_text_hash, state = item
    assert (extracted_text_hash, state) == (None, "unsummarizable")
    assert BlobStore(tmp_path / "blobs").get(content_hash) == raw_content
    assert [event[0] for event in events] == ["captured", "unsummarizable"]
    reason = json.loads(events[-1][1])
    assert reason["stage"] == "extraction"
    assert "no extractable text" in reason["reason"]


def test_add_records_a_corrupt_pdf_as_failed_without_rejecting_capture(
    tmp_path: Path,
) -> None:
    raw_content = b"%PDF-1.7\nnot actually a well-formed PDF"
    with serve(raw_content, content_type="application/pdf") as (url, _):
        result = run_modgud(tmp_path, "add", url)

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT content_hash, extracted_text_hash, state FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert result.returncode == 0, result.stderr
    assert item is not None
    content_hash, extracted_text_hash, state = item
    assert (extracted_text_hash, state) == (None, "failed")
    assert BlobStore(tmp_path / "blobs").get(content_hash) == raw_content
    assert [event[0] for event in events] == ["captured", "failed"]
    failure = json.loads(events[-1][1])
    assert failure["stage"] == "extraction"
    assert "pdf extraction failed" in failure["error"]


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
    with serve(raw_content, content_type="application/octet-stream") as (url, handler):
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


_ARTICLE_HTML = b"""
    <html>
      <head><title>Shipping a Software Factory</title></head>
      <body>
        <article>
          <h1>Shipping a Software Factory</h1>
          <p>We merged a thousand pull requests in a single week by letting
          agents own the whole loop from ticket to merge, and this post
          explains how the pipeline, the review gates, and the rollback
          policy were designed to keep that pace safe.</p>
          <p>The first lesson was that small, auditable changes matter more
          than raw throughput, because every merged change has to remain
          understandable to the humans who supervise the system.</p>
        </article>
      </body>
    </html>
"""
_STORED_URL = "https://example.com/stored"


def _store_item(
    data_dir: Path,
    *,
    content: bytes = _ARTICLE_HTML,
    item_format: str = "web",
    state: str = "failed",
    fetch_error: str | None = None,
    extracted_text: bytes | None = None,
) -> int:
    """Insert an item as an older capture would have left it: raw blob, no text."""
    blob_store = BlobStore(data_dir / "blobs")
    content_hash = blob_store.put(content)
    extracted_text_hash = (
        blob_store.put(extracted_text) if extracted_text is not None else None
    )
    with connect(data_dir / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash, format, state,
                source
            ) VALUES (?, ?, ?, ?, ?, 'example.com')
            """,
            (_STORED_URL, content_hash, extracted_text_hash, item_format, state),
        ).lastrowid
        assert item_id is not None
        ItemLog(connection, item_id).captured(
            url=_STORED_URL,
            canonical_url=_STORED_URL,
            origin="manual",
            fetch_error=fetch_error,
        )
    return item_id


class _ItemSnapshot(NamedTuple):
    state: str
    extracted_text_hash: str | None
    title: str | None
    author: str | None
    time_to_value_seconds: int | None
    source: str
    event_types: list[str]


def _snapshot(data_dir: Path, item_id: int) -> _ItemSnapshot:
    with connect(data_dir / "modgud.sqlite3") as connection:
        row = connection.execute(
            """
            SELECT state, extracted_text_hash, title, author,
                   time_to_value_seconds, source
            FROM items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        event_types = [
            event[0]
            for event in connection.execute(
                "SELECT type FROM events WHERE item_id = ? ORDER BY id", (item_id,)
            )
        ]
    state, extracted_text_hash, title, author, time_to_value, source = row
    return _ItemSnapshot(
        state,
        extracted_text_hash,
        title,
        author,
        time_to_value,
        source,
        event_types,
    )


def _reprocess(data_dir: Path, item_id: int) -> str:
    with connect(data_dir / "modgud.sqlite3") as connection:
        return reprocess_item(connection, BlobStore(data_dir / "blobs"), item_id)


def test_reprocess_extracts_a_pdf_stored_before_pdf_extraction_existed(
    tmp_path: Path,
) -> None:
    pdf = _pdf_bytes(
        "Smaller deployments reduce recovery time and limit operational risk.",
        title="A Study of Safe Deployments",
        author="Ada Rivera",
    )
    item_id = _store_item(
        tmp_path, content=pdf, item_format="pdf", state="unsummarizable"
    )

    assert _reprocess(tmp_path, item_id) == "extracted"

    item = _snapshot(tmp_path, item_id)
    assert (item.state, item.title, item.author) == (
        "extracted",
        "A Study of Safe Deployments",
        "Ada Rivera",
    )
    assert item.extracted_text_hash is not None
    extracted_text = BlobStore(tmp_path / "blobs").get(item.extracted_text_hash)
    assert b"Smaller deployments reduce recovery time" in extracted_text
    assert item.time_to_value_seconds is not None
    assert item.event_types == ["captured", "extracted"]


def test_reprocess_keeps_a_pdf_without_a_text_layer_unsummarizable(
    tmp_path: Path,
) -> None:
    item_id = _store_item(
        tmp_path,
        content=_pdf_bytes(None),
        item_format="pdf",
        state="unsummarizable",
    )

    assert _reprocess(tmp_path, item_id) == "unsummarizable"

    item = _snapshot(tmp_path, item_id)
    assert (item.state, item.extracted_text_hash) == ("unsummarizable", None)
    assert item.event_types == ["captured", "unsummarizable"]


def test_reprocess_retries_a_web_page_whose_extraction_failed(
    tmp_path: Path,
) -> None:
    html = _ARTICLE_HTML.replace(
        b"<head>", b'<head><meta property="og:site_name" content="Acme Blog">'
    )
    item_id = _store_item(tmp_path, content=html)

    assert _reprocess(tmp_path, item_id) == "extracted"

    item = _snapshot(tmp_path, item_id)
    assert (item.state, item.title, item.source) == (
        "extracted",
        "Shipping a Software Factory",
        "Acme Blog",
    )
    assert item.event_types == ["captured", "extracted"]


@pytest.mark.parametrize(
    ("item_format", "content"),
    [
        pytest.param("web", b"<html><body></body></html>", id="empty-page"),
        pytest.param("pdf", b"not a pdf", id="corrupt-pdf"),
    ],
)
def test_reprocess_records_content_that_still_cannot_be_extracted_as_failed(
    tmp_path: Path, item_format: str, content: bytes
) -> None:
    item_id = _store_item(
        tmp_path, content=content, item_format=item_format, state="captured"
    )

    assert _reprocess(tmp_path, item_id) == "failed"

    item = _snapshot(tmp_path, item_id)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        failure = json.loads(
            connection.execute(
                "SELECT payload FROM events WHERE type = 'failed'"
            ).fetchone()[0]
        )
    assert (item.state, item.extracted_text_hash) == ("failed", None)
    assert item.event_types == ["captured", "failed"]
    assert failure["stage"] == "extraction"
    assert failure["error"].startswith("ExtractionError: ")


_EARLIER_TEXT = b"Text from an earlier extraction."


@pytest.mark.parametrize(
    ("store", "expected_message"),
    [
        pytest.param(
            {"state": "extracted", "extracted_text": _EARLIER_TEXT},
            "already has extracted text",
            id="extracted",
        ),
        pytest.param(
            {"item_format": "pdf", "state": "failed", "extracted_text": _EARLIER_TEXT},
            "already has extracted text",
            id="summarization-failed",
        ),
        pytest.param({"item_format": "youtube"}, "only web and pdf", id="youtube"),
        pytest.param(
            {"item_format": "deck", "state": "unsummarizable"},
            "only web and pdf",
            id="deck",
        ),
        pytest.param(
            {"item_format": "pdf", "fetch_error": "URLError: unreachable"},
            "was never fetched",
            id="fetch-failed",
        ),
    ],
)
def test_reprocess_refuses_an_item_it_cannot_improve(
    tmp_path: Path, store: dict[str, Any], expected_message: str
) -> None:
    item_id = _store_item(tmp_path, **store)
    before = _snapshot(tmp_path, item_id)

    with pytest.raises(ReprocessError, match=expected_message):
        _reprocess(tmp_path, item_id)

    assert _snapshot(tmp_path, item_id) == before


def test_reprocess_reports_an_unknown_item(tmp_path: Path) -> None:
    with pytest.raises(ReprocessError, match="item 99 does not exist"):
        _reprocess(tmp_path, 99)


def test_reprocess_reports_stored_content_that_cannot_be_read(
    tmp_path: Path,
) -> None:
    item_id = _store_item(tmp_path)
    shutil.rmtree(tmp_path / "blobs")
    before = _snapshot(tmp_path, item_id)

    with pytest.raises(ReprocessError, match="stored content cannot be read"):
        _reprocess(tmp_path, item_id)

    assert _snapshot(tmp_path, item_id) == before


def test_reprocess_does_not_overwrite_an_item_that_changed_underneath_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    item_id = _store_item(tmp_path)

    class ConcurrentlySummarized(BlobStore):
        def get(self, blob_hash: str) -> bytes:
            with connect(database) as other:
                other.execute(
                    "UPDATE items SET state = 'summarized' WHERE id = ?", (item_id,)
                )
            return super().get(blob_hash)

    with (
        connect(database) as connection,
        pytest.raises(ReprocessError, match="changed while"),
    ):
        reprocess_item(connection, ConcurrentlySummarized(tmp_path / "blobs"), item_id)

    item = _snapshot(tmp_path, item_id)
    assert (item.state, item.extracted_text_hash) == ("summarized", None)
    assert item.event_types == ["captured"]


def test_reprocess_command_reports_the_new_state(tmp_path: Path) -> None:
    item_id = _store_item(tmp_path)

    result = run_modgud(tmp_path, "reprocess", str(item_id))

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"Reprocessed item {item_id}: extracted\n"


def test_reprocess_command_reports_a_refusal_on_stderr(tmp_path: Path) -> None:
    item_id = _store_item(tmp_path, extracted_text=_EARLIER_TEXT)

    result = run_modgud(tmp_path, "reprocess", str(item_id))

    assert result.returncode == 2
    assert f"item {item_id} already has extracted text" in result.stderr
    assert "Traceback" not in result.stderr
