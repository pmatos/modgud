"""Behavioral tests for delivering the morning digest."""

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from modgud.config import SecretValue
from modgud.database import connect
from modgud.delivery import (
    DigestDeliveryResult,
    DigestEmail,
    EmailClient,
    PostmarkDeliveryError,
    PostmarkEmailClient,
    deliver_digest,
)
from modgud.digests import select_digest_items


class RecordingEmailClient:
    """Capture messages at the external email-service boundary."""

    def __init__(self) -> None:
        self.messages: list[DigestEmail] = []

    def send_email(self, message: DigestEmail) -> str:
        self.messages.append(message)
        return "postmark-message-1"


def _deliver_digest(
    database: str | Path,
    client: EmailClient,
    *,
    scheduled_for: date | None = None,
) -> DigestDeliveryResult:
    return deliver_digest(
        database,
        client,
        from_address="modgud@example.com",
        to_address="reader@example.com",
        label_base_url="http://192.168.50.20:8000",
        label_signing_secret=SecretValue(
            "a-dedicated-test-secret-with-at-least-32-bytes"
        ),
        label_token_lifetime=timedelta(days=90),
        now=datetime(2026, 9, 3, 7, tzinfo=UTC),
        scheduled_for=scheduled_for,
    )


class _PostmarkHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, str], dict[str, object]]]] = []
    statuses: ClassVar[list[int]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        document = json.loads(self.rfile.read(content_length))
        type(self).requests.append((self.path, dict(self.headers), document))
        status = type(self).statuses.pop(0)
        response = json.dumps(
            {
                "ErrorCode": 0,
                "Message": "OK",
                "MessageID": "postmark-message-1",
                "To": document["To"],
            }
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve_postmark(
    statuses: list[int],
) -> Iterator[tuple[str, type[_PostmarkHandler]]]:
    class Handler(_PostmarkHandler):
        pass

    Handler.requests = []
    Handler.statuses = statuses.copy()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _add_visible_item(connection: sqlite3.Connection, position: int) -> int:
    cursor = connection.execute(
        """
        INSERT INTO items (
            canonical_url,
            content_hash,
            format,
            state,
            source,
            title,
            time_to_value_seconds
        ) VALUES (?, ?, 'web', 'failed', 'example.com', ?, ?)
        """,
        (
            f"https://example.com/{position}",
            f"content-{position}",
            f"Article {position}",
            position * 60,
        ),
    )
    item_id = cursor.lastrowid
    assert item_id is not None
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
        (item_id,),
    )
    return int(item_id)


def test_non_empty_digest_is_sent_and_records_the_exact_item_set(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item_ids = [_add_visible_item(connection, position) for position in (1, 2)]
    client = RecordingEmailClient()

    result = _deliver_digest(database, client)

    with connect(database) as connection:
        sent_events = connection.execute(
            "SELECT item_id, payload FROM events WHERE type = 'digest_sent'"
        ).fetchall()
    assert result.sent
    assert result.item_ids == tuple(item_ids)
    assert len(client.messages) == 1
    message = client.messages[0]
    assert message.from_address == "modgud@example.com"
    assert message.to_address == "reader@example.com"
    assert message.subject == "modgud digest"
    assert "Article 1" in message.text_body
    assert "Article 2" in message.html_body
    assert sent_events[0][0] == item_ids[0]
    assert json.loads(sent_events[0][1]) == {
        "item_ids": item_ids,
        "postmark_message_id": "postmark-message-1",
    }


def test_each_digest_item_has_signed_worth_it_and_not_worth_it_links(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item_id = _add_visible_item(connection, 1)
    client = RecordingEmailClient()

    deliver_digest(
        database,
        client,
        from_address="modgud@example.com",
        to_address="reader@example.com",
        label_base_url="http://192.168.50.20:8000",
        label_signing_secret=SecretValue("a-dedicated-test-secret-with-32-bytes"),
        label_token_lifetime=timedelta(days=90),
        now=datetime(2026, 9, 3, 7, tzinfo=UTC),
    )

    message = client.messages[0]
    html_links = re.findall(r'href="([^"]+)"', unescape(message.html_body))
    label_links = [link for link in html_links if "/labels/" in link]
    assert len(label_links) == 2
    for label in ("worth-it", "not-worth-it"):
        prefix = f"http://192.168.50.20:8000/items/{item_id}/labels/{label}?token="
        matching_links = [link for link in label_links if link.startswith(prefix)]
        assert len(matching_links) == 1
        assert matching_links[0] in message.text_body


def test_empty_selection_sends_nothing_and_records_no_send_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    client = RecordingEmailClient()

    result = _deliver_digest(database, client)

    with connect(database) as connection:
        sent_event_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'digest_sent'"
        ).fetchone()[0]
    assert not result.sent
    assert result.item_ids == ()
    assert client.messages == []
    assert sent_event_count == 0


def test_postmark_retries_transient_failures_with_the_same_multipart_email() -> None:
    delays: list[float] = []
    email = DigestEmail(
        from_address="modgud@example.com",
        to_address="reader@example.com",
        subject="modgud digest",
        html_body="<p>The digest</p>",
        text_body="The digest",
    )

    with serve_postmark([500, 503, 200]) as (base_url, handler):
        client = PostmarkEmailClient(
            SecretValue("server-token-secret"),
            base_url=base_url,
            sleep=delays.append,
        )
        message_id = client.send_email(email)

    assert message_id == "postmark-message-1"
    assert delays == [1.0, 2.0]
    assert len(handler.requests) == 3
    for path, headers, document in handler.requests:
        assert path == "/email"
        assert headers["X-Postmark-Server-Token"] == "server-token-secret"
        assert document == {
            "From": "modgud@example.com",
            "HtmlBody": "<p>The digest</p>",
            "MessageStream": "outbound",
            "Subject": "modgud digest",
            "TextBody": "The digest",
            "To": "reader@example.com",
        }


def test_exhausted_send_retries_leave_no_event_and_preserve_the_selection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        item_id = _add_visible_item(connection, 1)

    with serve_postmark([500, 500, 500, 500]) as (base_url, handler):
        client = PostmarkEmailClient(
            SecretValue("server-token-secret"),
            base_url=base_url,
            sleep=lambda _delay: None,
        )
        with pytest.raises(
            PostmarkDeliveryError,
            match=r"failed after 4 attempts \(HTTP 500\)",
        ):
            _deliver_digest(database, client)

    with connect(database) as connection:
        sent_event_count = connection.execute(
            "SELECT count(*) FROM events WHERE type = 'digest_sent'"
        ).fetchone()[0]
        selected_ids = tuple(item.id for item in select_digest_items(connection))
    assert len(handler.requests) == 4
    assert sent_event_count == 0
    assert selected_ids == (item_id,)


def test_a_completed_scheduled_day_does_not_send_new_items_again(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    with connect(database) as connection:
        first_item = _add_visible_item(connection, 1)
    client = RecordingEmailClient()

    first = _deliver_digest(database, client, scheduled_for=date(2026, 9, 3))
    with connect(database) as connection:
        second_item = _add_visible_item(connection, 2)
    repeated = _deliver_digest(database, client, scheduled_for=date(2026, 9, 3))

    with connect(database) as connection:
        selected_ids = tuple(item.id for item in select_digest_items(connection))
        scheduled_runs = connection.execute(
            "SELECT local_date, outcome FROM digest_schedule"
        ).fetchall()
    assert first.sent
    assert first.item_ids == (first_item,)
    assert not repeated.sent
    assert repeated.item_ids == ()
    assert len(client.messages) == 1
    assert selected_ids == (second_item,)
    assert scheduled_runs == [("2026-09-03", "sent")]


def test_an_empty_scheduled_day_is_completed_without_sending(
    tmp_path: Path,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    client = RecordingEmailClient()

    empty_run = _deliver_digest(database, client, scheduled_for=date(2026, 9, 3))
    with connect(database) as connection:
        next_item = _add_visible_item(connection, 1)
    repeated = _deliver_digest(database, client, scheduled_for=date(2026, 9, 3))

    with connect(database) as connection:
        scheduled_runs = connection.execute(
            "SELECT local_date, outcome FROM digest_schedule"
        ).fetchall()
        selected_ids = tuple(item.id for item in select_digest_items(connection))
    assert not empty_run.sent
    assert not repeated.sent
    assert client.messages == []
    assert scheduled_runs == [("2026-09-03", "empty")]
    assert selected_ids == (next_item,)
