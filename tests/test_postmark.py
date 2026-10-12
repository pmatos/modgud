"""Contract tests for the Postmark request module's domain operations."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.error import HTTPError

import pytest

from modgud.config import SecretValue
from modgud.postmark import (
    DigestEmail,
    PostmarkClient,
    PostmarkDeliveryError,
    PostmarkEmailClient,
    PostmarkError,
)

_TOKEN = "server-token-secret"


class _Handler(BaseHTTPRequestHandler):
    response_bodies: ClassVar[list[tuple[int, bytes]]] = []
    requests: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).requests.append((self.command, self.path, dict(self.headers), body))
        status, response = type(self).response_bodies.pop(0)
        self.send_response(status)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _serve(
    responses: list[tuple[int, bytes]],
) -> Iterator[tuple[str, type[_Handler]]]:
    class Handler(_Handler):
        pass

    Handler.response_bodies = responses.copy()
    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _email() -> DigestEmail:
    return DigestEmail(
        from_address="modgud@example.com",
        to_address="reader@example.com",
        subject="modgud digest",
        html_body="<p>The digest</p>",
        text_body="The digest",
    )


def test_domain_operations_share_authentication_and_retry_policy() -> None:
    delays: list[float] = []
    with _serve(
        [
            (503, b"unavailable"),
            (200, b"[]"),
            (200, b'{"TotalCount": 0, "InboundMessages": []}'),
            (200, b'{"MessageID": "message/id ?"}'),
            (200, b'{"ErrorCode": 422, "MessageID": "rejected"}'),
            (200, b'{"ErrorCode": 0, "MessageID": "outbound"}'),
        ]
    ) as (base_url, handler):
        inbound = PostmarkClient(
            SecretValue(_TOKEN), base_url=f"{base_url}///", sleep=delays.append
        )
        outbound = PostmarkEmailClient(
            SecretValue(_TOKEN), base_url=base_url, sleep=delays.append
        )

        search = inbound.search_inbound(count=500, offset=10)
        details = inbound.get_inbound("message/id ?")
        message_id = outbound.send_email(_email())

    assert search == {"TotalCount": 0, "InboundMessages": []}
    assert details == {"MessageID": "message/id ?"}
    assert message_id == "outbound"
    assert delays == [1.0, 2.0, 1.0]
    assert len(handler.requests) == 6
    for method, path, headers, body in handler.requests:
        assert headers["X-Postmark-Server-Token"] == _TOKEN
        assert headers["Accept"] == "application/json"
        assert headers["User-Agent"] == "modgud/0.1"
        if method == "GET":
            assert body == b""
            assert "Content-Type" not in headers
        else:
            assert method == "POST"
            assert path == "/email"
            assert headers["Content-Type"] == "application/json"
            assert json.loads(body) == {
                "From": "modgud@example.com",
                "HtmlBody": "<p>The digest</p>",
                "MessageStream": "outbound",
                "Subject": "modgud digest",
                "TextBody": "The digest",
                "To": "reader@example.com",
            }
    assert [request[1] for request in handler.requests[:3]] == [
        "/messages/inbound?count=500&offset=10&status=processed"
    ] * 3
    assert handler.requests[3][1] == "/messages/inbound/message%2Fid%20%3F/details"
    assert handler.requests[4] == handler.requests[5]


@pytest.mark.parametrize(
    ("status", "body", "detail", "cause_type"),
    [
        (503, b"server-token-secret", "HTTP 503", HTTPError),
        (200, b"[]", "TypeError", TypeError),
        (200, b"\xff", "UnicodeDecodeError", UnicodeDecodeError),
        (200, b"not JSON server-token-secret", "JSONDecodeError", json.JSONDecodeError),
    ],
)
def test_exhaustion_reports_safe_failure_details(
    status: int, body: bytes, detail: str, cause_type: type[Exception]
) -> None:
    delays: list[float] = []
    with _serve([(status, body)] * 4) as (base_url, handler):
        client = PostmarkClient(
            SecretValue(_TOKEN), base_url=base_url, sleep=delays.append
        )
        with pytest.raises(PostmarkError) as raised:
            client.search_inbound(count=500, offset=0)

    assert str(raised.value) == (
        f"Postmark API request failed after 4 attempts ({detail})"
    )
    assert isinstance(raised.value.__cause__, cause_type)
    assert _TOKEN not in str(raised.value)
    assert delays == [1.0, 2.0, 4.0]
    assert len(handler.requests) == 4


def test_rejected_email_keeps_the_delivery_error_and_retry_limit() -> None:
    delays: list[float] = []
    with _serve(
        [(200, b'{"ErrorCode": 422, "Message": "server-token-secret"}')] * 4
    ) as (base_url, handler):
        client = PostmarkEmailClient(
            SecretValue(_TOKEN), base_url=base_url, sleep=delays.append
        )
        with pytest.raises(PostmarkDeliveryError) as raised:
            client.send_email(_email())

    assert str(raised.value) == (
        "Postmark email send failed after 4 attempts (ValueError)"
    )
    assert isinstance(raised.value.__cause__, ValueError)
    assert delays == [1.0, 2.0, 4.0]
    assert len(handler.requests) == 4
