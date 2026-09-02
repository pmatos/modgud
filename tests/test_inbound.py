"""Behavioral tests for polling Postmark inbound messages."""

import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from modgud.cli import main
from modgud.config import SecretValue
from modgud.database import connect
from modgud.inbound import PostmarkClient, PostmarkError, poll_inbound


class FakePostmarkClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.detail_requests: list[str] = []
        self.search_requests: list[tuple[int, int]] = []

    def search_inbound(self, *, count: int, offset: int) -> dict[str, Any]:
        self.search_requests.append((count, offset))
        return {
            "TotalCount": len(self.messages),
            "InboundMessages": self.messages[offset : offset + count],
        }

    def get_inbound(self, message_id: str) -> dict[str, Any]:
        self.detail_requests.append(message_id)
        return next(
            message for message in self.messages if message["MessageID"] == message_id
        )


class FailingDetailsClient(FakePostmarkClient):
    def get_inbound(self, message_id: str) -> dict[str, Any]:
        self.detail_requests.append(message_id)
        raise PostmarkError("Postmark API request failed after 4 attempts (HTTP 503)")


class MismatchedDetailsClient(FakePostmarkClient):
    def get_inbound(self, message_id: str) -> dict[str, Any]:
        self.detail_requests.append(message_id)
        return {"MessageID": "a-different-message", "TextBody": "wrong body"}


class _RetryingPostmarkHandler(BaseHTTPRequestHandler):
    request_count = 0
    requested_paths: ClassVar[list[str]] = []
    received_tokens: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        type(self).request_count += 1
        type(self).requested_paths.append(self.path)
        type(self).received_tokens.append(self.headers.get("X-Postmark-Server-Token"))
        if type(self).request_count < 3:
            self.send_error(503)
            return
        body = json.dumps({"TotalCount": 0, "InboundMessages": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve_retrying_postmark() -> Iterator[tuple[str, type[_RetryingPostmarkHandler]]]:
    handler = _RetryingPostmarkHandler
    handler.request_count = 0
    handler.requested_paths = []
    handler.received_tokens = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_poll_retrieves_and_durably_queues_a_new_inbound_message(
    tmp_path: Path,
) -> None:
    message = {
        "MessageID": "message-1",
        "From": "reader@example.com",
        "Subject": "Worth reading",
        "TextBody": "https://example.com/useful",
        "HtmlBody": "",
        "Headers": [],
    }
    client = FakePostmarkClient([message])
    database = tmp_path / "modgud.sqlite3"

    result = poll_inbound(
        database,
        client,
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        stored = connection.execute(
            "SELECT message_id, payload FROM postmark_inbound_messages"
        ).fetchone()

    assert result.new_message_count == 1
    assert result.skipped is False
    assert client.detail_requests == ["message-1"]
    assert stored == ("message-1", json.dumps(message, separators=(",", ":")))


def test_durable_message_cursor_prevents_reprocessing_after_restart(
    tmp_path: Path,
) -> None:
    message = {"MessageID": "message-1", "TextBody": "https://example.com/once"}
    database = tmp_path / "modgud.sqlite3"
    first_client = FakePostmarkClient([message])
    second_client = FakePostmarkClient([message])

    poll_inbound(
        database,
        first_client,
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )
    result = poll_inbound(
        database,
        second_client,
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, 2, tzinfo=UTC),
    )

    with connect(database) as connection:
        queued_count = connection.execute(
            "SELECT count(*) FROM postmark_inbound_messages"
        ).fetchone()[0]

    assert result.new_message_count == 0
    assert second_client.detail_requests == []
    assert queued_count == 1


def test_poll_retrieves_every_page_of_new_messages(tmp_path: Path) -> None:
    messages = [
        {"MessageID": f"message-{position}", "TextBody": f"body {position}"}
        for position in range(501)
    ]
    client = FakePostmarkClient(messages)
    database = tmp_path / "modgud.sqlite3"

    result = poll_inbound(
        database,
        client,
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        queued_count = connection.execute(
            "SELECT count(*) FROM postmark_inbound_messages"
        ).fetchone()[0]

    assert result.new_message_count == 501
    assert client.search_requests == [(500, 0), (500, 500)]
    assert len(client.detail_requests) == 501
    assert queued_count == 501


def test_configured_interval_controls_when_the_timer_polls_again(
    tmp_path: Path,
) -> None:
    first = {"MessageID": "message-1", "TextBody": "first"}
    second = {"MessageID": "message-2", "TextBody": "second"}
    database = tmp_path / "modgud.sqlite3"
    started_at = datetime(2026, 9, 2, 12, tzinfo=UTC)

    poll_inbound(
        database,
        FakePostmarkClient([first]),
        poll_interval=timedelta(minutes=2),
        now=started_at,
    )
    early_client = FakePostmarkClient([second, first])
    early = poll_inbound(
        database,
        early_client,
        poll_interval=timedelta(minutes=2),
        now=started_at + timedelta(minutes=1),
    )
    due_client = FakePostmarkClient([second, first])
    due = poll_inbound(
        database,
        due_client,
        poll_interval=timedelta(minutes=2),
        now=started_at + timedelta(minutes=2),
    )

    assert early.skipped is True
    assert early_client.search_requests == []
    assert due.skipped is False
    assert due.new_message_count == 1
    assert due_client.detail_requests == ["message-2"]


def test_postmark_api_errors_are_retried_with_exponential_backoff() -> None:
    delays: list[float] = []

    with serve_retrying_postmark() as (base_url, handler):
        client = PostmarkClient(
            SecretValue("server-token"),
            base_url=base_url,
            sleep=delays.append,
        )

        response = client.search_inbound(count=500, offset=0)

    assert response == {"TotalCount": 0, "InboundMessages": []}
    assert delays == [1.0, 2.0]
    assert handler.requested_paths == [
        "/messages/inbound?count=500&offset=0&status=processed",
        "/messages/inbound?count=500&offset=0&status=processed",
        "/messages/inbound?count=500&offset=0&status=processed",
    ]
    assert handler.received_tokens == ["server-token"] * 3


def test_failed_api_request_preserves_the_previous_cursor_for_retry(
    tmp_path: Path,
) -> None:
    first = {"MessageID": "message-1", "TextBody": "first"}
    second = {"MessageID": "message-2", "TextBody": "second"}
    database = tmp_path / "modgud.sqlite3"
    started_at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    poll_inbound(
        database,
        FakePostmarkClient([first]),
        poll_interval=timedelta(minutes=2),
        now=started_at,
    )

    with pytest.raises(PostmarkError, match="after 4 attempts"):
        poll_inbound(
            database,
            FailingDetailsClient([second, first]),
            poll_interval=timedelta(minutes=2),
            now=started_at + timedelta(minutes=2),
        )

    retry_client = FakePostmarkClient([second, first])
    retry = poll_inbound(
        database,
        retry_client,
        poll_interval=timedelta(minutes=2),
        now=started_at + timedelta(minutes=2),
    )

    with connect(database) as connection:
        queued_ids = [
            row[0]
            for row in connection.execute(
                "SELECT message_id FROM postmark_inbound_messages ORDER BY message_id"
            )
        ]

    assert retry.new_message_count == 1
    assert retry_client.detail_requests == ["message-2"]
    assert queued_ids == ["message-1", "message-2"]


def test_poll_inbound_is_a_one_shot_cli_command_using_the_configured_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    client = FakePostmarkClient([])
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token")
    monkeypatch.setattr("modgud.cli.PostmarkClient", lambda token: client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modgud",
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "poll-inbound",
        ],
    )

    main()
    main()
    sys.argv.append("--force")
    main()

    assert capsys.readouterr().out == (
        "Polled Postmark: 0 new inbound messages\n"
        "Inbound poll is not due yet\n"
        "Polled Postmark: 0 new inbound messages\n"
    )
    assert client.search_requests == [(500, 0), (500, 0)]


def test_mismatched_message_details_do_not_advance_the_cursor(tmp_path: Path) -> None:
    database = tmp_path / "modgud.sqlite3"
    client = MismatchedDetailsClient([{"MessageID": "message-1"}])

    with pytest.raises(PostmarkError, match="different message ID"):
        poll_inbound(
            database,
            client,
            poll_interval=timedelta(minutes=2),
            now=datetime(2026, 9, 2, 12, tzinfo=UTC),
        )

    with connect(database) as connection:
        queued_count = connection.execute(
            "SELECT count(*) FROM postmark_inbound_messages"
        ).fetchone()[0]
        completed_count = connection.execute(
            "SELECT count(*) FROM postmark_inbound_poll_state"
        ).fetchone()[0]

    assert queued_count == 0
    assert completed_count == 0
