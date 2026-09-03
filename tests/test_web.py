"""Behavioral tests for the LAN web application."""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modgud.cli import main
from modgud.config import ConfigError, get_settings
from modgud.database import connect
from modgud.web import create_app, serve


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
