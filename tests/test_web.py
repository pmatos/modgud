"""Behavioral tests for the LAN web application."""

import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modgud.cli import main
from modgud.config import ConfigError, Settings, get_settings
from modgud.database import connect
from modgud.digests import DigestItem, render_digest
from modgud.formats import ItemFormat
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


def _settings_with_label_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_lifetime_days: int = 90,
) -> Settings:
    repository = Path(__file__).parents[1]
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (repository / "config.example.toml")
        .read_text(encoding="utf-8")
        .replace(
            "token_lifetime_days = 90",
            f"token_lifetime_days = {token_lifetime_days}",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "LABEL_TOKEN_SECRET", "a-dedicated-test-secret-with-at-least-32-bytes"
    )
    return get_settings(config_path)


def _signed_label_target(
    item_id: int,
    label: str,
    *,
    settings: Settings,
    now: datetime,
) -> str:
    signing_secret = settings.secrets.label_token_secret
    assert signing_secret is not None
    rendered = render_digest(
        (
            DigestItem(
                id=item_id,
                canonical_url=f"https://example.com/{item_id}",
                format=ItemFormat.WEB,
                state="failed",
                source="example.com",
                title=f"Item {item_id}",
                author=None,
                time_to_value_seconds=None,
                summary=None,
            ),
        ),
        label_base_url=settings.web_bind.base_url,
        label_signing_secret=signing_secret,
        label_token_lifetime=settings.label_token_lifetime,
        now=now,
    )
    match = re.search(
        rf'href="([^"]+/items/{item_id}/labels/{label}\?token=[^"]+)"',
        rendered.html,
    )
    assert match is not None
    parts = urlsplit(unescape(match.group(1)))
    return f"{parts.path}?{parts.query}"


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


def test_a_digest_token_cannot_be_reused_for_another_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        items = [
            connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, format, state, source, title
                ) VALUES (?, ?, 'web', 'summarized', 'example.com', ?)
                """,
                (
                    f"https://example.com/{position}",
                    f"content-{position}",
                    f"Item {position}",
                ),
            ).lastrowid
            for position in (1, 2)
        ]
    first_item, second_item = items
    assert first_item is not None
    assert second_item is not None
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    signed_target = _signed_label_target(
        first_item,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    replayed_target = signed_target.replace(
        f"/items/{first_item}/", f"/items/{second_item}/"
    )

    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.post(replayed_target)

    assert response.status_code == 400
    assert "not valid for this item" in response.text
    assert "No label was recorded" in response.text
    with connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE type = 'label'"
            ).fetchone()[0]
            == 0
        )


def test_a_digest_token_cannot_be_reused_for_the_opposite_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title
            ) VALUES (
                'https://example.com/scoped', 'scoped', 'web', 'summarized',
                'example.com', 'A scoped item'
            )
            """
        ).lastrowid
    assert item_id is not None
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    worth_it_target = _signed_label_target(
        item_id,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    replayed_target = worth_it_target.replace(
        "/labels/worth-it?", "/labels/not-worth-it?"
    )

    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.post(replayed_target)

    assert response.status_code == 400
    assert "not valid for this opinion" in response.text
    assert "No label was recorded" in response.text
    with connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE type = 'label'"
            ).fetchone()[0]
            == 0
        )


def test_a_modified_digest_token_cannot_record_a_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title
            ) VALUES (
                'https://example.com/tampered', 'tampered', 'web', 'summarized',
                'example.com', 'A tampered item'
            )
            """
        ).lastrowid
    assert item_id is not None
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    signed_target = _signed_label_target(
        item_id,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    replacement = "A" if signed_target[-1] != "A" else "B"
    modified_target = f"{signed_target[:-1]}{replacement}"

    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.post(modified_target)

    assert response.status_code == 400
    assert "invalid or incomplete" in response.text
    assert "No label was recorded" in response.text
    with connect(database) as connection:
        label_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'label'"
        ).fetchone()[0]
    assert label_count == 0


def test_an_expired_digest_token_fails_with_an_explanation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title
            ) VALUES (
                'https://example.com/old', 'old', 'web', 'summarized',
                'example.com', 'An old item'
            )
            """
        ).lastrowid
    assert item_id is not None
    settings = _settings_with_label_secret(
        tmp_path,
        monkeypatch,
        token_lifetime_days=2,
    )
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    signed_target = _signed_label_target(
        item_id,
        "worth-it",
        settings=settings,
        now=issued_at,
    )

    with TestClient(
        create_app(
            tmp_path,
            settings=settings,
            clock=lambda: issued_at + timedelta(days=3),
        )
    ) as client:
        response = client.get(signed_target)

    assert response.status_code == 410
    assert "expired" in response.text.lower()
    assert "No label was recorded" in response.text


def test_label_link_asks_for_confirmation_without_recording_a_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title
            ) VALUES (
                'https://example.com/useful', 'useful', 'web', 'summarized',
                'example.com', 'A useful article'
            )
            """
        )

    item_id = item.lastrowid
    assert item_id is not None
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    target = _signed_label_target(
        item_id,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.get(target)

    assert response.status_code == 200
    assert "A useful article" in response.text
    assert re.search(r"worth it", response.text, re.IGNORECASE)
    assert re.search(r'<form[^>]+method="post"', response.text)
    with connect(database) as connection:
        label_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'label'"
        ).fetchone()[0]
    assert label_count == 0


def test_confirming_a_label_records_it_in_the_event_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source, title
            ) VALUES (
                'https://example.com/useful', 'useful', 'web', 'summarized',
                'example.com', 'A useful article'
            )
            """
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
            (item.lastrowid,),
        )

    item_id = item.lastrowid
    assert item_id is not None
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    target = _signed_label_target(
        item_id,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.post(target)

    assert response.status_code == 200
    assert "Label recorded" in response.text
    with connect(database) as connection:
        events = connection.execute(
            """
            SELECT item_id, type, json_extract(payload, '$.label'),
                   created_at IS NOT NULL
            FROM events
            ORDER BY id
            """
        ).fetchall()
    assert events == [
        (item.lastrowid, "captured", None, 1),
        (item.lastrowid, "label", "worth-it", 1),
    ]


def test_relabelling_appends_history_while_an_unlabelled_item_stays_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        labelled = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/labelled', 'labelled', 'web',
                    'summarized', 'example.com')
            """
        )
        unlabelled = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/unlabelled', 'unlabelled', 'web',
                    'summarized', 'example.com')
            """
        )

    labelled_id = labelled.lastrowid
    assert labelled_id is not None
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    worth_it_target = _signed_label_target(
        labelled_id,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    not_worth_it_target = _signed_label_target(
        labelled_id,
        "not-worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    with TestClient(create_app(tmp_path, settings=settings)) as client:
        first = client.post(worth_it_target)
        second = client.post(not_worth_it_target)

    assert first.status_code == 200
    assert second.status_code == 200
    with connect(database) as connection:
        labels = connection.execute(
            """
            SELECT item_id, json_extract(payload, '$.label')
            FROM events
            WHERE type = 'label'
            ORDER BY id
            """
        ).fetchall()
    assert labels == [
        (labelled.lastrowid, "worth-it"),
        (labelled.lastrowid, "not-worth-it"),
    ]
    assert all(item_id != unlabelled.lastrowid for item_id, _ in labels)


def test_a_label_link_for_a_missing_item_fails_without_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    settings = _settings_with_label_secret(tmp_path, monkeypatch)
    target = _signed_label_target(
        404,
        "worth-it",
        settings=settings,
        now=datetime.now(UTC),
    )
    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.get(target)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Item not found" in response.text
    assert "No label was recorded" in response.text
    with connect(database) as connection:
        label_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'label'"
        ).fetchone()[0]
    assert label_count == 0


def test_an_unknown_label_fails_without_recording(tmp_path: Path) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/item', 'item', 'web', 'summarized',
                    'example.com')
            """
        )

    with TestClient(create_app(tmp_path)) as client:
        response = client.post(f"/items/{item.lastrowid}/labels/maybe")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Label not recognized" in response.text
    assert "No label was recorded" in response.text
    with connect(database) as connection:
        label_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'label'"
        ).fetchone()[0]
    assert label_count == 0


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
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (repository / "config.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "LABEL_TOKEN_SECRET", "a-dedicated-test-secret-with-at-least-32-bytes"
    )
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
            str(config_path),
            "--data-dir",
            str(tmp_path),
            "serve",
        ],
    )

    main()

    assert len(launched) == 1
