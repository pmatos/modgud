"""Behavioral tests for the LAN web application."""

import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modgud.cli import main
from modgud.config import ConfigError, get_settings
from modgud.database import connect
from modgud.web import create_app, serve


@contextmanager
def serve_pdf() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"%PDF-1.7\nlocal test document"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/document.pdf"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_drop_box_captures_an_item_visible_to_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = Path(__file__).parents[1]
    with (
        serve_pdf() as url,
        TestClient(create_app(tmp_path)) as client,
    ):
        response = client.post("/", data={"url": url}, follow_redirects=True)

    assert response.status_code == 200
    assert re.search(r"Captured\s+item 1", response.text)
    assert url in response.text

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modgud",
            "--config",
            str(repository / "config.example.toml"),
            "--data-dir",
            str(tmp_path),
            "list",
        ],
    )
    main()

    assert "1    pdf" in capsys.readouterr().out


def test_drop_box_reports_an_existing_item_for_a_duplicate(tmp_path: Path) -> None:
    with (
        serve_pdf() as url,
        TestClient(create_app(tmp_path)) as client,
    ):
        first = client.post("/", data={"url": url}, follow_redirects=True)
        duplicate = client.post("/", data={"url": url}, follow_redirects=True)

    assert re.search(r"Captured\s+item 1", first.text)
    assert re.search(r"Already captured as\s+item 1", duplicate.text)
    assert 'aria-label="1 item captured"' in duplicate.text
    assert url in duplicate.text


def test_drop_box_rejects_a_non_http_url_without_capturing_it(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/", data={"url": "not a URL"})

    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "Enter a complete HTTP or HTTPS URL." in response.text
    assert 'aria-label="0 items captured"' in response.text


def test_item_list_shows_capture_details(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title,
                time_to_value_seconds
            ) VALUES (
                'https://example.com/state-machines', 'state-machines', 'web',
                'summarized', 'Engineering Notes', 'State Machines That Last', 125
            )
            """
        )
        connection.execute(
            """
            INSERT INTO events (item_id, type, payload, created_at)
            VALUES (?, 'captured', '{}', '2026-09-03T08:30:00.000Z')
            """,
            (item.lastrowid,),
        )

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "State Machines That Last" in response.text
    assert re.search(r">\s*web\s*<", response.text)
    assert re.search(r">\s*summarized\s*<", response.text)
    assert 'datetime="2026-09-03T08:30:00.000Z"' in response.text
    assert re.search(r">\s*3 min\s*<", response.text)


def test_item_list_filters_by_format(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        for url, content_hash, item_format, title in (
            ("https://example.com/article", "article", "web", "A web article"),
            ("https://example.com/paper.pdf", "paper", "pdf", "A PDF paper"),
        ):
            item = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, format, state, source, title
                ) VALUES (?, ?, ?, 'unsummarizable', 'example.com', ?)
                """,
                (url, content_hash, item_format, title),
            )
            connection.execute(
                "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
                (item.lastrowid,),
            )

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/?format=pdf")

    assert response.status_code == 200
    assert "A PDF paper" in response.text
    assert "A web article" not in response.text
    assert "Not available" in response.text
    assert re.search(r'<option value="pdf"\s+selected>', response.text)


def test_item_list_filters_by_design_lifecycle_state(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        for url, content_hash, state, title in (
            ("https://example.com/ready", "ready", "summarized", "Ready to read"),
            ("https://example.com/error", "error", "failed", "Needs attention"),
        ):
            item = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, format, state, source, title
                ) VALUES (?, ?, 'web', ?, 'example.com', ?)
                """,
                (url, content_hash, state, title),
            )
            connection.execute(
                "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
                (item.lastrowid,),
            )

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/?state=failed")

    assert response.status_code == 200
    assert "Needs attention" in response.text
    assert "Ready to read" not in response.text
    assert re.search(r'<option value="failed"\s+selected>', response.text)
    for state in (
        "captured",
        "extracted",
        "summarized",
        "unsummarizable",
        "failed",
    ):
        assert f'value="{state}"' in response.text


def test_home_page_reports_items_from_the_cli_store(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'content', 'web',
                    'captured', 'example.com')
            """
        )

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>modgud</title>" in response.text
    assert "1 item captured" in response.text


def test_home_page_uses_a_served_stylesheet_without_client_javascript(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(tmp_path)) as client:
        page = client.get("/")
        stylesheet = client.get("/static/modgud.css")

    assert 'href="http://testserver/static/modgud.css"' in page.text
    assert "<script" not in page.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "--surface:" in stylesheet.text


def test_server_uses_the_configured_lan_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).parents[1]
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (repository / "config.example.toml")
        .read_text(encoding="utf-8")
        .replace('bind = "127.0.0.1:8000"', 'bind = "192.168.50.20:8123"'),
        encoding="utf-8",
    )
    settings = get_settings(config_path)
    launched: dict[str, object] = {}

    def record_launch(app: object, *, host: str, port: int) -> None:
        launched.update(app=app, host=host, port=port)

    monkeypatch.setattr("modgud.web.uvicorn.run", record_launch)

    serve(settings, tmp_path / "data")

    assert (launched["host"], launched["port"]) == ("192.168.50.20", 8123)
    assert launched["host"] != "0.0.0.0"
    launched_app = launched["app"]
    assert isinstance(launched_app, FastAPI)
    with TestClient(launched_app) as client:
        response = client.get("/")
    assert 'aria-label="0 items captured"' in response.text


def test_wildcard_ipv4_bind_is_rejected(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (repository / "config.example.toml")
        .read_text(encoding="utf-8")
        .replace('bind = "127.0.0.1:8000"', 'bind = "0.0.0.0:8000"'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"web\.bind cannot use a wildcard host"):
        get_settings(config_path)


def test_wildcard_ipv6_bind_is_rejected(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (repository / "config.example.toml")
        .read_text(encoding="utf-8")
        .replace('bind = "127.0.0.1:8000"', 'bind = "[::]:8000"'),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"web\.bind cannot use a wildcard host"):
        get_settings(config_path)


def test_serve_command_starts_the_web_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).parents[1]
    launched: list[object] = []

    def record_launch(app: object, *, host: str, port: int) -> None:
        launched.append((app, host, port))

    monkeypatch.setattr("modgud.web.uvicorn.run", record_launch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modgud",
            "--config",
            str(repository / "config.example.toml"),
            "--data-dir",
            str(tmp_path),
            "serve",
        ],
    )

    main()

    assert len(launched) == 1
