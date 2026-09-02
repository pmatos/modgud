import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from modgud.database import connect


class _ResponseHandler(BaseHTTPRequestHandler):
    body = b""
    content_type = "application/octet-stream"
    request_count = 0

    def do_GET(self) -> None:
        type(self).request_count += 1
        self.send_response(200)
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
) -> Iterator[tuple[str, type[_ResponseHandler]]]:
    class Handler(_ResponseHandler):
        pass

    Handler.body = body
    Handler.content_type = content_type
    Handler.request_count = 0
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
