"""Retrieve Postmark inbound messages into the durable processing queue."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from modgud.config import SecretValue
from modgud.database import connect

_PAGE_SIZE = 500
_POSTMARK_API_URL = "https://api.postmarkapp.com"
_RETRY_DELAYS = (1.0, 2.0, 4.0)


class PostmarkError(RuntimeError):
    """Postmark could not return a usable API response."""


class PostmarkClient:
    """Small adapter for the Postmark inbound Messages API."""

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

    def search_inbound(self, *, count: int, offset: int) -> Mapping[str, Any]:
        query = urlencode({"count": count, "offset": offset, "status": "processed"})
        return self._get_json(f"/messages/inbound?{query}")

    def get_inbound(self, message_id: str) -> Mapping[str, Any]:
        encoded_id = quote(message_id, safe="")
        return self._get_json(f"/messages/inbound/{encoded_id}/details")

    def _get_json(self, path: str) -> Mapping[str, Any]:
        request = Request(
            f"{self._base_url}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "modgud/0.1",
                "X-Postmark-Server-Token": self._server_token.reveal(),
            },
        )
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                with urlopen(request, timeout=30) as response:
                    document = json.loads(response.read())
                if not isinstance(document, dict):
                    raise TypeError("response is not a JSON object")
                return cast("dict[str, Any]", document)
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
                    raise PostmarkError(
                        "Postmark API request failed after "
                        f"{attempt + 1} attempts ({detail})"
                    ) from error
                self._sleep(_RETRY_DELAYS[attempt])

        raise AssertionError("unreachable")


class InboundClient(Protocol):
    """The Postmark operations required by one inbound poll."""

    def search_inbound(self, *, count: int, offset: int) -> Mapping[str, Any]: ...

    def get_inbound(self, message_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PollResult:
    """Observable outcome of one scheduled poll attempt."""

    new_message_count: int
    skipped: bool


def poll_inbound(
    database: str | Path,
    client: InboundClient,
    *,
    poll_interval: timedelta,
    now: datetime,
    force: bool = False,
) -> PollResult:
    """Retrieve and queue every previously unseen inbound message."""
    new_message_count = 0
    with connect(database) as connection:
        previous_poll = connection.execute(
            """
            SELECT completed_at
            FROM postmark_inbound_poll_state
            WHERE singleton = 1
            """
        ).fetchone()
        if previous_poll is not None and not force:
            completed_at = datetime.fromisoformat(str(previous_poll[0]))
            if now < completed_at + poll_interval:
                return PollResult(new_message_count=0, skipped=True)

        offset = 0
        total_count = 1
        while offset < total_count:
            response = client.search_inbound(count=_PAGE_SIZE, offset=offset)
            total_count_value = response.get("TotalCount")
            summaries = response.get("InboundMessages")
            if not isinstance(total_count_value, int) or total_count_value < 0:
                raise ValueError("Postmark inbound search response has no total count")
            if not isinstance(summaries, list):
                raise TypeError("Postmark inbound search response has no message list")
            total_count = total_count_value

            for summary in summaries:
                if not isinstance(summary, Mapping):
                    raise TypeError(
                        "Postmark inbound search returned an invalid message"
                    )
                message_id = summary.get("MessageID")
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError(
                        "Postmark inbound search returned a message without an ID"
                    )
                already_queued = connection.execute(
                    """
                    SELECT 1
                    FROM postmark_inbound_messages
                    WHERE message_id = ?
                    """,
                    (message_id,),
                ).fetchone()
                if already_queued is not None:
                    continue
                details = client.get_inbound(message_id)
                if details.get("MessageID") != message_id:
                    raise PostmarkError(
                        "Postmark returned details for a different message ID"
                    )
                payload = json.dumps(dict(details), separators=(",", ":"))
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO postmark_inbound_messages (
                        message_id,
                        payload
                    ) VALUES (?, ?)
                    """,
                    (message_id, payload),
                )
                new_message_count += inserted.rowcount

            offset += len(summaries)
            if offset < total_count and not summaries:
                raise ValueError("Postmark inbound search pagination made no progress")
        connection.execute(
            """
            INSERT INTO postmark_inbound_poll_state (singleton, completed_at)
            VALUES (1, ?)
            ON CONFLICT (singleton) DO UPDATE SET completed_at = excluded.completed_at
            """,
            (now.isoformat(),),
        )

    return PollResult(new_message_count=new_message_count, skipped=False)
