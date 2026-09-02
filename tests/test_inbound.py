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
from modgud.inbound import (
    PostmarkClient,
    PostmarkError,
    pending_inbound_captures,
    poll_inbound,
)


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


class _CapturedTargetHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"%PDF-1.7\nA captured document"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
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


@contextmanager
def serve_captured_target() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturedTargetHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/paper.pdf"
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


def test_plain_text_url_and_newsletter_origin_are_extracted_durably(
    tmp_path: Path,
) -> None:
    message = {
        "MessageID": "message-1",
        "From": "delivery@example.net",
        "TextBody": "Saved for later: https://example.com/useful?ref=inbox",
        "HtmlBody": "",
        "Headers": [
            {
                "Name": "List-Id",
                "Value": "Systems Weekly <systems-weekly.example.net>",
            }
        ],
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        extracted = connection.execute(
            """
            SELECT target_url, origin, processed_at, item_id
            FROM postmark_inbound_messages
            """
        ).fetchone()

    assert extracted == (
        "https://example.com/useful?ref=inbox",
        "systems-weekly.example.net",
        None,
        None,
    )


def test_html_url_is_used_when_plain_text_has_no_usable_url(tmp_path: Path) -> None:
    message = {
        "MessageID": "message-1",
        "TextBody": "The saved article is linked in the HTML version.",
        "HtmlBody": (
            '<html><body><a href="https://example.com/from-html?utm_medium=email">'
            "Read the article</a></body></html>"
        ),
        "Headers": [{"Name": "List-Id", "Value": "<engineering-weekly.example>"}],
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        target_url = connection.execute(
            "SELECT target_url FROM postmark_inbound_messages"
        ).fetchone()[0]

    assert target_url == "https://example.com/from-html?utm_medium=email"


def test_visible_web_url_is_extracted_from_html_without_an_anchor(
    tmp_path: Path,
) -> None:
    message = {
        "MessageID": "message-1",
        "TextBody": "",
        "HtmlBody": (
            "<html><head><style>"
            "@import url(https://assets.example/fonts.css);"
            "</style></head><body>"
            "<p>Read https://example.com/visible.</p>"
            "</body></html>"
        ),
        "Headers": [],
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        target_url = connection.execute(
            "SELECT target_url FROM postmark_inbound_messages"
        ).fetchone()[0]

    assert target_url == "https://example.com/visible"


@pytest.mark.parametrize(
    "forwarded_marker",
    [
        "---------- Forwarded message ---------",
        "Begin forwarded message:",
        "-----Original Message-----",
    ],
)
def test_forwarded_envelope_supplies_the_original_sender_as_origin(
    tmp_path: Path,
    forwarded_marker: str,
) -> None:
    message = {
        "MessageID": "message-1",
        "From": "me@example.com",
        "TextBody": f"""A good one.

{forwarded_marker}
From: Deep Systems <letters@deep-systems.example>
Date: Wed, 2 Sep 2026 09:15:00 +0200
Subject: Queues are coordination
To: me@example.com

Read https://articles.example/queues for the argument.
""",
        "HtmlBody": "",
        "Headers": [],
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        extracted = connection.execute(
            "SELECT target_url, origin FROM postmark_inbound_messages"
        ).fetchone()

    assert extracted == (
        "https://articles.example/queues",
        "letters@deep-systems.example",
    )


def test_html_forwarded_envelope_supplies_the_original_sender_as_origin(
    tmp_path: Path,
) -> None:
    message = {
        "MessageID": "message-1",
        "From": "me@example.com",
        "TextBody": "",
        "HtmlBody": """
            <div>---------- Forwarded message ---------</div>
            <div>From: Research Notes &lt;dispatch@research.example&gt;</div>
            <div>Date: Wed, 2 Sep 2026 09:15:00 +0200</div>
            <div>Subject: Evidence first</div>
            <p><a href="https://articles.example/evidence">Read it</a></p>
        """,
        "Headers": [],
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        extracted = connection.execute(
            "SELECT target_url, origin FROM postmark_inbound_messages"
        ).fetchone()

    assert extracted == (
        "https://articles.example/evidence",
        "dispatch@research.example",
    )


@pytest.mark.parametrize(
    "sender_fields",
    [
        {"From": "Ava Reader <AVA@EXAMPLE.COM>", "Headers": []},
        {"FromFull": {"Name": "Ava Reader", "Email": "AVA@EXAMPLE.COM"}, "Headers": []},
        {"Headers": [{"Name": "From", "Value": "Ava Reader <AVA@EXAMPLE.COM>"}]},
    ],
)
def test_sender_mailbox_is_the_origin_when_no_newsletter_header_exists(
    tmp_path: Path,
    sender_fields: dict[str, Any],
) -> None:
    message = {
        "MessageID": "message-1",
        "TextBody": "https://example.com/recommended",
        "HtmlBody": "",
        **sender_fields,
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        origin = connection.execute(
            "SELECT origin FROM postmark_inbound_messages"
        ).fetchone()[0]

    assert origin == "ava@example.com"


def test_message_without_a_usable_url_is_retained_with_unknown_origin(
    tmp_path: Path,
) -> None:
    message = {
        "MessageID": "message-without-url",
        "TextBody": (
            "A note with malformed https://[broken and "
            "ftp://files.example/archive but no web link."
        ),
        "HtmlBody": '<a href="mailto:editor@example.com">Reply</a>',
        "Headers": [],
    }
    database = tmp_path / "modgud.sqlite3"
    received_at = datetime(2026, 9, 2, 12, tzinfo=UTC)

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=received_at,
    )

    with connect(database) as connection:
        stored = connection.execute(
            """
            SELECT message_id, payload, target_url, origin, processed_at, item_id
            FROM postmark_inbound_messages
            """
        ).fetchone()

    assert stored == (
        "message-without-url",
        json.dumps(message, separators=(",", ":")),
        None,
        None,
        received_at.isoformat(),
        None,
    )


def test_first_plain_text_web_url_wins_when_a_message_contains_multiple_urls(
    tmp_path: Path,
) -> None:
    message = {
        "MessageID": "message-1",
        "From": "reader@example.com",
        "TextBody": (
            "Primary: https://first.example/article\n"
            "Also mentioned: https://second.example/related"
        ),
        "HtmlBody": '<a href="https://html.example/alternative">HTML link</a>',
        "Headers": [],
    }
    database = tmp_path / "modgud.sqlite3"

    poll_inbound(
        database,
        FakePostmarkClient([message]),
        poll_interval=timedelta(minutes=2),
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
    )

    with connect(database) as connection:
        target_url = connection.execute(
            "SELECT target_url FROM postmark_inbound_messages"
        ).fetchone()[0]

    assert target_url == "https://first.example/article"


def test_poll_command_captures_extracted_url_with_email_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "server-token")

    with serve_captured_target() as target_url:
        client = FakePostmarkClient(
            [
                {
                    "MessageID": "message-1",
                    "From": "curator@example.com",
                    "TextBody": f"Worth reading: {target_url}",
                    "HtmlBody": "",
                    "Headers": [],
                }
            ]
        )
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

    with connect(data_dir / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT id, canonical_url, format, state, source FROM items"
        ).fetchone()
        capture = json.loads(
            connection.execute(
                "SELECT payload FROM events WHERE type = 'captured'"
            ).fetchone()[0]
        )
        queued = connection.execute(
            """
            SELECT target_url, origin, processed_at, item_id
            FROM postmark_inbound_messages
            """
        ).fetchone()

    assert item == (1, target_url, "pdf", "unsummarizable", "127.0.0.1")
    assert capture == {
        "canonical_url": target_url,
        "inbound_message_id": "message-1",
        "origin": "curator@example.com",
        "url": target_url,
    }
    assert queued[:2] == (target_url, "curator@example.com")
    assert queued[2] is not None
    assert queued[3] == 1


def test_messages_queued_before_extraction_was_added_are_enriched(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    messages = [
        {
            "MessageID": "legacy-with-url",
            "From": "curator@example.com",
            "TextBody": "https://example.com/from-existing-queue",
            "HtmlBody": "",
            "Headers": [],
        },
        {
            "MessageID": "legacy-without-url",
            "TextBody": "Remember to review this later.",
            "HtmlBody": "",
            "Headers": [],
        },
    ]
    with connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO postmark_inbound_messages (message_id, payload)
            VALUES (?, ?)
            """,
            [
                (message["MessageID"], json.dumps(message, separators=(",", ":")))
                for message in messages
            ],
        )

    pending = pending_inbound_captures(database)

    with connect(database) as connection:
        stored = connection.execute(
            """
            SELECT message_id, target_url, origin, processed_at
            FROM postmark_inbound_messages
            ORDER BY message_id
            """
        ).fetchall()

    assert [
        (capture.message_id, capture.target_url, capture.origin) for capture in pending
    ] == [
        (
            "legacy-with-url",
            "https://example.com/from-existing-queue",
            "curator@example.com",
        )
    ]
    assert stored[0][:3] == (
        "legacy-with-url",
        "https://example.com/from-existing-queue",
        "curator@example.com",
    )
    assert stored[0][3] is None
    assert stored[1][:3] == ("legacy-without-url", None, None)
    assert stored[1][3] is not None


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
