"""Deliver rendered digests through an external email service."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from modgud.config import SecretValue
from modgud.database import connect
from modgud.digests import render_digest, select_digest_items

_POSTMARK_API_URL = "https://api.postmarkapp.com"
_RETRY_DELAYS = (1.0, 2.0, 4.0)


@dataclass(frozen=True, slots=True)
class DigestEmail:
    """One self-contained digest ready for an email service."""

    from_address: str
    to_address: str
    subject: str
    html_body: str
    text_body: str


class EmailClient(Protocol):
    """The external email operation required by digest delivery."""

    def send_email(self, message: DigestEmail) -> str: ...


class PostmarkDeliveryError(RuntimeError):
    """Postmark could not accept an outbound digest."""


class PostmarkEmailClient:
    """Small adapter for Postmark's single-email API."""

    def __init__(
        self,
        server_token: SecretValue,
        *,
        base_url: str = _POSTMARK_API_URL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._server_token = server_token
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep

    def send_email(self, message: DigestEmail) -> str:
        """Submit one multipart digest and return Postmark's message ID."""
        body = json.dumps(
            {
                "From": message.from_address,
                "HtmlBody": message.html_body,
                "MessageStream": "outbound",
                "Subject": message.subject,
                "TextBody": message.text_body,
                "To": message.to_address,
            },
            separators=(",", ":"),
        ).encode()
        request = Request(
            f"{self._base_url}/email",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "modgud/0.1",
                "X-Postmark-Server-Token": self._server_token.reveal(),
            },
            method="POST",
        )
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                with urlopen(request, timeout=30) as response:
                    document = json.loads(response.read())
                if not isinstance(document, dict):
                    raise TypeError("response is not a JSON object")
                response_object = cast("dict[str, Any]", document)
                if response_object.get("ErrorCode") != 0:
                    raise ValueError("Postmark rejected the message")
                message_id = response_object.get("MessageID")
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError("Postmark response has no message ID")
                return message_id
            except (
                HTTPError,
                URLError,
                TimeoutError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                if attempt == len(_RETRY_DELAYS):
                    detail = (
                        f"HTTP {error.code}"
                        if isinstance(error, HTTPError)
                        else type(error).__name__
                    )
                    raise PostmarkDeliveryError(
                        "Postmark email send failed after "
                        f"{attempt + 1} attempts ({detail})"
                    ) from error
                self._sleep(_RETRY_DELAYS[attempt])

        raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class DigestDeliveryResult:
    """Observable outcome of one digest delivery attempt."""

    sent: bool
    item_ids: tuple[int, ...]


def deliver_digest(
    database: str | Path,
    client: EmailClient,
    *,
    from_address: str,
    to_address: str,
    scheduled_for: date | None = None,
) -> DigestDeliveryResult:
    """Send the currently eligible item set and advance its event boundary."""
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if scheduled_for is not None:
            completed = connection.execute(
                "SELECT 1 FROM digest_schedule WHERE local_date = ?",
                (scheduled_for.isoformat(),),
            ).fetchone()
            if completed is not None:
                return DigestDeliveryResult(sent=False, item_ids=())
        items = select_digest_items(connection)
        item_ids = tuple(item.id for item in items)
        if not item_ids:
            if scheduled_for is not None:
                connection.execute(
                    """
                    INSERT INTO digest_schedule (local_date, outcome)
                    VALUES (?, 'empty')
                    """,
                    (scheduled_for.isoformat(),),
                )
            return DigestDeliveryResult(sent=False, item_ids=())
        rendered = render_digest(items)
        message_id = client.send_email(
            DigestEmail(
                from_address=from_address,
                to_address=to_address,
                subject="modgud digest",
                html_body=rendered.html,
                text_body=rendered.text,
            )
        )
        payload = json.dumps(
            {
                "item_ids": item_ids,
                "postmark_message_id": message_id,
                **(
                    {"scheduled_for": scheduled_for.isoformat()}
                    if scheduled_for is not None
                    else {}
                ),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO events (item_id, type, payload)
            VALUES (?, 'digest_sent', ?)
            """,
            (item_ids[0], payload),
        )
        if scheduled_for is not None:
            connection.execute(
                """
                INSERT INTO digest_schedule (local_date, outcome)
                VALUES (?, 'sent')
                """,
                (scheduled_for.isoformat(),),
            )
    return DigestDeliveryResult(sent=True, item_ids=item_ids)
