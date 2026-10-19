"""Retrieve Postmark inbound messages and replay the durable Capture queue."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from modgud.capture import CaptureResult, capture_url
from modgud.config import Settings
from modgud.database import connect
from modgud.postmark import PostmarkClient, PostmarkError

__all__ = [
    "InboundClient",
    "InboundExtraction",
    "PendingInboundCapture",
    "PollResult",
    "PostmarkClient",
    "PostmarkError",
    "extract_inbound_message",
    "pending_inbound_captures",
    "poll_inbound",
    "run_inbound_poll",
]

_PAGE_SIZE = 500
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_FORWARDED_MARKER = re.compile(
    r"^(?:-+\s*)?(?:(?:begin\s+)?forwarded|original) message"
    r"(?:\s*-+)?\s*:?[ \t]*$",
    re.IGNORECASE,
)


class InboundClient(Protocol):
    """The Postmark operations required by one inbound poll."""

    def search_inbound(self, *, count: int, offset: int) -> Mapping[str, Any]: ...

    def get_inbound(self, message_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PollResult:
    """Observable outcome of one scheduled poll attempt."""

    new_message_count: int
    skipped: bool


@dataclass(frozen=True, slots=True)
class InboundExtraction:
    """Capture information recovered from one inbound message."""

    target_url: str | None
    origin: str | None


@dataclass(frozen=True, slots=True)
class PendingInboundCapture:
    """A queued message whose extracted URL has not entered the item pipeline."""

    message_id: str
    target_url: str
    origin: str | None


def run_inbound_poll(
    data_dir: Path,
    client: InboundClient,
    settings: Settings,
    *,
    now: datetime,
    on_capture: Callable[[CaptureResult], None],
    force: bool = False,
) -> PollResult:
    """Poll when due, then replay pending Captures even when polling is skipped.

    Polling commits before replay; each Capture owns its transaction. Notify
    the caller synchronously after each completed Capture, before starting the
    next. Poll, Capture, and notification failures propagate, leaving earlier
    progress durable and later messages pending.
    """
    database = data_dir / "modgud.sqlite3"
    result = poll_inbound(
        database,
        client,
        poll_interval=settings.inbound_poll_interval,
        now=now,
        force=force,
    )
    for pending in pending_inbound_captures(database):
        captured = capture_url(
            data_dir,
            pending.target_url,
            settings,
            origin=pending.origin,
            inbound_message_id=pending.message_id,
        )
        if captured is not None:
            on_capture(captured)
    return result


class _HtmlLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.text: list[str] = []
        self.ignored_element: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() in {"script", "style"}:
            self.ignored_element = tag.casefold()
            return
        if self.ignored_element is not None:
            return
        if tag.casefold() in {"br", "div", "li", "p", "tr"}:
            self.text.append("\n")
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value is not None:
                self.urls.extend(_usable_urls(value))
                return

    def handle_data(self, data: str) -> None:
        if self.ignored_element is not None:
            return
        self.text.append(data)
        self.urls.extend(_usable_urls(data))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == self.ignored_element:
            self.ignored_element = None
            return
        if self.ignored_element is not None:
            return
        if tag.casefold() in {"div", "li", "p", "tr"}:
            self.text.append("\n")


def _html_content(html: str) -> tuple[list[str], str]:
    parser = _HtmlLinks()
    parser.feed(html)
    parser.close()
    return parser.urls, "".join(parser.text)


def _usable_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        candidate = match.group().rstrip(".,;:!?)]}")
        try:
            parts = urlsplit(candidate)
        except ValueError:
            continue
        if parts.scheme.lower() in {"http", "https"} and parts.hostname is not None:
            urls.append(candidate)
    return urls


def _header_value(message: Mapping[str, Any], name: str) -> str | None:
    headers = message.get("Headers")
    if not isinstance(headers, list):
        return None
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        header_name = header.get("Name")
        value = header.get("Value")
        if (
            isinstance(header_name, str)
            and header_name.casefold() == name.casefold()
            and isinstance(value, str)
            and value.strip()
        ):
            return value.strip()
    return None


def _list_origin(message: Mapping[str, Any]) -> str | None:
    value = _header_value(message, "List-Id")
    if value is None:
        return None
    identifier = re.search(r"<([^<>]+)>", value)
    return (identifier.group(1) if identifier is not None else value).strip().lower()


def _mailbox(value: str) -> str | None:
    address = parseaddr(value)[1].strip().lower()
    if "@" not in address or any(character.isspace() for character in address):
        return None
    return address


def _forwarded_origin(text: str) -> str | None:
    origin = None
    lines = [re.sub(r"^\s*>\s?", "", line) for line in text.splitlines()]
    for position, line in enumerate(lines):
        if _FORWARDED_MARKER.fullmatch(line.strip()) is None:
            continue
        for header in lines[position + 1 :]:
            if header.casefold().startswith("from:"):
                origin = _mailbox(header.partition(":")[2])
                break
            if not header.strip():
                continue
    return origin


def _sender_origin(message: Mapping[str, Any]) -> str | None:
    full_sender = message.get("FromFull")
    if isinstance(full_sender, Mapping):
        email = full_sender.get("Email")
        if isinstance(email, str) and (origin := _mailbox(email)) is not None:
            return origin
    for sender in (message.get("From"), _header_value(message, "From")):
        if isinstance(sender, str) and (origin := _mailbox(sender)) is not None:
            return origin
    return None


def extract_inbound_message(message: Mapping[str, Any]) -> InboundExtraction:
    """Recover the target URL and stable origin exposed by a message."""
    text_body = message.get("TextBody")
    urls = _usable_urls(text_body) if isinstance(text_body, str) else []
    html_body = message.get("HtmlBody")
    html_urls, html_text = (
        _html_content(html_body) if isinstance(html_body, str) else ([], "")
    )
    if not urls:
        urls = html_urls
    forwarded_origin = (
        _forwarded_origin(text_body) if isinstance(text_body, str) else None
    )
    if forwarded_origin is None:
        forwarded_origin = _forwarded_origin(html_text)
    return InboundExtraction(
        target_url=urls[0] if urls else None,
        origin=forwarded_origin or _list_origin(message) or _sender_origin(message),
    )


def pending_inbound_captures(
    database: str | Path,
) -> tuple[PendingInboundCapture, ...]:
    """Return extracted URLs still awaiting durable item capture."""
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        unextracted = connection.execute(
            """
            SELECT message_id, payload
            FROM postmark_inbound_messages
            WHERE target_url IS NULL AND processed_at IS NULL
            """
        ).fetchall()
        for message_id, payload in unextracted:
            message = json.loads(str(payload))
            if not isinstance(message, dict):
                raise TypeError("Queued inbound payload is not a JSON object")
            extraction = extract_inbound_message(message)
            connection.execute(
                """
                UPDATE postmark_inbound_messages
                SET target_url = ?,
                    origin = ?,
                    processed_at = CASE
                        WHEN ? IS NULL
                        THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ELSE NULL
                    END
                WHERE message_id = ? AND processed_at IS NULL
                """,
                (
                    extraction.target_url,
                    extraction.origin,
                    extraction.target_url,
                    str(message_id),
                ),
            )
        rows = connection.execute(
            """
            SELECT message_id, target_url, origin
            FROM postmark_inbound_messages
            WHERE target_url IS NOT NULL AND processed_at IS NULL
            ORDER BY queued_at, message_id
            """
        ).fetchall()
    return tuple(
        PendingInboundCapture(
            message_id=str(row[0]),
            target_url=str(row[1]),
            origin=str(row[2]) if row[2] is not None else None,
        )
        for row in rows
    )


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
                extraction = extract_inbound_message(details)
                payload = json.dumps(dict(details), separators=(",", ":"))
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO postmark_inbound_messages (
                        message_id,
                        payload,
                        target_url,
                        origin,
                        processed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        payload,
                        extraction.target_url,
                        extraction.origin,
                        now.isoformat() if extraction.target_url is None else None,
                    ),
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
