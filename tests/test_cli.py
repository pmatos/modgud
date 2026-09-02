import json
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from modgud.blobs import BlobStore
from modgud.database import connect


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
    assert stored_blobs == [raw_content]


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
            connection.execute("SELECT count(*) FROM events").fetchone()[0],
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
