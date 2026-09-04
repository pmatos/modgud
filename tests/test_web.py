"""Behavioral tests for the LAN web application."""

import json
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modgud.blobs import BlobStore
from modgud.cli import main
from modgud.config import ConfigError, Settings, get_settings
from modgud.database import connect
from modgud.digests import DigestItem, render_digest
from modgud.formats import ItemFormat
from modgud.transcripts import chunk_transcript
from modgud.web import create_app, serve
from modgud.youtube import Chapter


def _vtt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _vtt_transcript(cues: Sequence[tuple[int, int, str]]) -> bytes:
    lines = ["WEBVTT", ""]
    for start_ms, end_ms, text in cues:
        lines.append(f"{_vtt_timestamp(start_ms)} --> {_vtt_timestamp(end_ms)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).encode("utf-8")


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


def test_item_transcript_renders_chunk_text_with_timestamps(tmp_path: Path) -> None:
    transcript = _vtt_transcript(
        [
            (0, 2_500, "Opening context worth knowing."),
            (65_000, 95_000, "The core tradeoff is introduced here."),
        ]
    )
    chapters: list[Chapter] = [
        {"start_time": 0.0, "end_time": 60.0, "title": "Opening"},
        {"start_time": 60.0, "end_time": 95.0, "title": "Core"},
    ]
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, title, chapters
            ) VALUES (?, ?, ?, 'youtube', 'summarized', ?, ?, ?)
            """,
            (
                "https://www.youtube.com/watch?v=transcript",
                "transcript-item",
                transcript_hash,
                "Practical Channel",
                "A worthwhile conversation",
                json.dumps(chapters),
            ),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert "A worthwhile conversation" in response.text
    assert "Practical Channel" in response.text
    assert "Opening context worth knowing." in response.text
    assert "The core tradeoff is introduced here." in response.text
    assert "01:05" in response.text
    assert 'id="t-0"' in response.text
    assert 'id="t-65000"' in response.text


def test_item_transcript_anchors_match_span_map_start_times(tmp_path: Path) -> None:
    transcript = _vtt_transcript(
        [
            (0, 2_000, "First worthwhile part."),
            (10_000, 12_000, "Second worthwhile part."),
        ]
    )
    chapters: list[Chapter] = [
        {"start_time": 0.0, "end_time": 9.0, "title": "First"},
        {"start_time": 9.0, "end_time": 13.0, "title": "Second"},
    ]
    chunks = chunk_transcript(transcript, chapters=chapters)
    assert len(chunks) == 2
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, chapters
            ) VALUES (
                'https://example.com/podcasts/anchor', 'anchor-item', ?,
                'podcast', 'summarized', 'Practical Podcast', ?
            )
            """,
            (transcript_hash, json.dumps(chapters)),
        ).lastrowid
        assert item_id is not None
        connection.execute("INSERT INTO span_maps (item_id) VALUES (?)", (item_id,))
        connection.executemany(
            """
            INSERT INTO span_map_spans (
                item_id, position, start_ms, end_ms, description
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (item_id, position, chunk.start_ms, chunk.end_ms, "Worth a look.")
                for position, chunk in enumerate(chunks)
            ],
        )

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    for chunk in chunks:
        assert f'id="t-{chunk.start_ms}"' in response.text


def test_item_transcript_deduplicates_anchors_for_a_repeated_start_time(
    tmp_path: Path,
) -> None:
    long_text = " ".join(f"Sentence {position}." for position in range(700))
    transcript = _vtt_transcript([(0, 7_200_000, long_text)])
    chunks = chunk_transcript(transcript)
    assert len(chunks) > 1
    assert all(chunk.start_ms == 0 for chunk in chunks)
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (
                'https://example.com/podcasts/single-cue', 'single-cue-item', ?,
                'podcast', 'summarized', 'Practical Podcast'
            )
            """,
            (transcript_hash,),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert response.text.count('id="t-0"') == 1
    assert len(re.findall(r"<li[ >]", response.text)) == len(chunks)


def test_item_transcript_bounds_a_two_hour_transcript_to_structural_chunks(
    tmp_path: Path,
) -> None:
    cues = [
        (position * 5_000, position * 5_000 + 4_000, f"Segment {position} content.")
        for position in range(1_440)
    ]
    transcript = _vtt_transcript(cues)
    expected_chunks = chunk_transcript(transcript)
    assert 1 < len(expected_chunks) < 100
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (
                'https://example.com/podcasts/long', 'long-item', ?,
                'podcast', 'summarized', 'Practical Podcast'
            )
            """,
            (transcript_hash,),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert len(re.findall(r"<li[ >]", response.text)) == len(expected_chunks)


def test_item_transcript_404_for_a_missing_item(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/items/404")

    assert response.status_code == 404
    assert "Item not found" in response.text


def test_item_transcript_404_when_format_has_no_transcript(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/article', 'article', 'web',
                    'summarized', 'example.com')
            """
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 404
    assert "No transcript is available" in response.text


def test_item_transcript_404_when_not_yet_transcribed(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://www.youtube.com/watch?v=pending', 'pending', 'youtube',
                    'extracted', 'Practical Channel')
            """
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 404
    assert "No transcript is available" in response.text


def test_item_transcript_404_when_the_stored_blob_is_missing(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (
                'https://www.youtube.com/watch?v=missing-blob', 'missing-blob', ?,
                'youtube', 'summarized', 'Practical Channel'
            )
            """,
            ("c" * 64,),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 404
    assert "No transcript is available" in response.text


def test_item_transcript_404_when_chapters_are_malformed(tmp_path: Path) -> None:
    transcript = _vtt_transcript([(0, 2_000, "Some content worth reading.")])
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, chapters
            ) VALUES (
                'https://www.youtube.com/watch?v=bad-chapters', 'bad-chapters', ?,
                'youtube', 'summarized', 'Practical Channel', ?
            )
            """,
            (transcript_hash, json.dumps({"not": "a list"})),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 404
    assert "No transcript is available" in response.text


def _settings_for_tier_2_endpoint(tmp_path: Path, endpoint: str) -> Settings:
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / f"tier-2-web-config-{urlsplit(endpoint).port}.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8").replace(
            """[models.tier_2_summary]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b\"""",
            f"""[models.tier_2_summary]
base_url = "{endpoint}"
model = "gemma4:26b-a4b\"""",
        ),
        encoding="utf-8",
    )
    return get_settings(config_path)


class _BlockingCompletionHandler(BaseHTTPRequestHandler):
    request_count: ClassVar[int] = 0
    release: ClassVar[threading.Event]
    content: ClassVar[str]

    def do_POST(self) -> None:
        type(self).request_count += 1
        content_length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(content_length))
        type(self).release.wait(timeout=5)
        body = json.dumps(
            {
                "id": "long-form",
                "object": "chat.completion",
                "created": 0,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": type(self).content,
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve_blocking_completion(
    release: threading.Event, content: str
) -> Iterator[tuple[str, type[_BlockingCompletionHandler]]]:
    class Handler(_BlockingCompletionHandler):
        pass

    Handler.request_count = 0
    Handler.release = release
    Handler.content = content
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_long_form_summary_page_offers_to_generate_for_an_eligible_item(
    tmp_path: Path,
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An article about durable queues.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, title
            ) VALUES (
                'https://example.com/queues', 'queues-item', ?,
                'web', 'summarized', 'example.com', 'Durable Queues'
            )
            """,
            (extracted_text_hash,),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}/summary")

    assert response.status_code == 200
    assert "Durable Queues" in response.text
    assert re.search(r'<form[^>]+method="post"', response.text)
    assert "Generate long-form summary" in response.text


def test_long_form_summary_page_explains_when_none_is_possible(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/deck.pdf', 'deck-item', 'pdf',
                    'unsummarizable', 'example.com')
            """
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}/summary")

    assert response.status_code == 200
    assert "No long-form summary is available" in response.text
    assert "Generate long-form summary" not in response.text


def test_a_pending_summary_left_over_from_a_restart_surfaces_as_failed(
    tmp_path: Path,
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An article about crash recovery.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (
                'https://example.com/crash-recovery', 'crash-recovery-item', ?,
                'web', 'summarized', 'example.com'
            )
            """,
            (extracted_text_hash,),
        ).lastrowid
        assert item_id is not None
        connection.execute(
            "INSERT INTO tier_2_summaries (item_id, status) VALUES (?, 'pending')",
            (item_id,),
        )

    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/items/{item_id}/summary")

    assert response.status_code == 200
    assert "Generating your long-form summary" not in response.text
    assert "could not be generated" in response.text
    assert "Try again" in response.text


def test_long_form_summary_page_404_for_a_missing_item(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/items/404/summary")

    assert response.status_code == 404
    assert "Item not found" in response.text


def test_requesting_a_summary_for_a_missing_item_fails_without_recording(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/items/404/summary")

    assert response.status_code == 404
    assert "Item not found" in response.text


def test_requesting_a_summary_without_model_routing_fails_clearly(
    tmp_path: Path,
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An article about routing.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (
                'https://example.com/routing', 'routing-item', ?,
                'web', 'summarized', 'example.com'
            )
            """,
            (extracted_text_hash,),
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path)) as client:
        response = client.post(f"/items/{item_id}/summary")

    assert response.status_code == 500
    assert "Model routing is not configured" in response.text


def test_requesting_a_summary_for_an_ineligible_item_fails_without_starting_work(
    tmp_path: Path,
) -> None:
    config_path = Path(__file__).parents[1] / "config.example.toml"
    settings = get_settings(config_path)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES ('https://example.com/deck.pdf', 'deck-item-2', 'pdf',
                    'unsummarizable', 'example.com')
            """
        ).lastrowid
    assert item_id is not None

    with TestClient(create_app(tmp_path, settings=settings)) as client:
        response = client.post(f"/items/{item_id}/summary")

    assert response.status_code == 400
    assert "No content is available to summarize" in response.text


def test_requesting_a_summary_shows_pending_then_completed_and_is_free_twice(
    tmp_path: Path,
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An article about backpressure.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, title
            ) VALUES (
                'https://example.com/backpressure', 'backpressure-item', ?,
                'web', 'summarized', 'example.com', 'Backpressure'
            )
            """,
            (extracted_text_hash,),
        ).lastrowid
    assert item_id is not None

    release = threading.Event()
    long_form_text = "A patient walk through why backpressure keeps producers honest."
    with serve_blocking_completion(release, long_form_text) as (endpoint, handler):
        settings = _settings_for_tier_2_endpoint(tmp_path, endpoint)
        with TestClient(create_app(tmp_path, settings=settings)) as client:
            first = client.post(f"/items/{item_id}/summary", follow_redirects=True)
            second = client.post(f"/items/{item_id}/summary", follow_redirects=True)

            assert first.status_code == 200
            assert "Generating your long-form summary" in first.text
            assert "Generating your long-form summary" in second.text

            release.set()
            completed = _wait_until(
                lambda: (
                    "Generating your long-form summary"
                    not in client.get(f"/items/{item_id}/summary").text
                )
            )
            assert completed
            final = client.get(f"/items/{item_id}/summary")

    assert long_form_text in final.text
    assert handler.request_count == 1


def test_a_failed_summary_can_be_retried_and_then_completes(tmp_path: Path) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An article about retries.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item_id = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, title
            ) VALUES (
                'https://example.com/retries', 'retries-item', ?,
                'web', 'summarized', 'example.com', 'Retries'
            )
            """,
            (extracted_text_hash,),
        ).lastrowid
    assert item_id is not None

    release = threading.Event()
    release.set()
    with serve_blocking_completion(release, "   ") as (endpoint, _):
        settings = _settings_for_tier_2_endpoint(tmp_path, endpoint)
        with TestClient(create_app(tmp_path, settings=settings)) as client:
            client.post(f"/items/{item_id}/summary", follow_redirects=True)
            failed = _wait_until(
                lambda: (
                    "could not be generated"
                    in client.get(f"/items/{item_id}/summary").text
                )
            )
            assert failed
            failure_page = client.get(f"/items/{item_id}/summary")

    assert "Try again" in failure_page.text

    release2 = threading.Event()
    release2.set()
    long_form_text = "A settled account after a retry."
    with serve_blocking_completion(release2, long_form_text) as (endpoint, _):
        settings = _settings_for_tier_2_endpoint(tmp_path, endpoint)
        with TestClient(create_app(tmp_path, settings=settings)) as client:
            client.post(f"/items/{item_id}/summary", follow_redirects=True)
            completed = _wait_until(
                lambda: long_form_text in client.get(f"/items/{item_id}/summary").text
            )
            assert completed
