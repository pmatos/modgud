"""Deliver rendered digests through an external email service."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from modgud.config import SecretValue
from modgud.database import connect
from modgud.digests import render_digest, select_digest_items
from modgud.events import ItemLog
from modgud.postmark import DigestEmail, PostmarkDeliveryError, PostmarkEmailClient

__all__ = [
    "DigestDeliveryResult",
    "DigestEmail",
    "EmailClient",
    "PostmarkDeliveryError",
    "PostmarkEmailClient",
    "deliver_digest",
]


class EmailClient(Protocol):
    """The external email operation required by digest delivery."""

    def send_email(self, message: DigestEmail) -> str: ...


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
    label_base_url: str,
    label_signing_secret: SecretValue,
    label_token_lifetime: timedelta,
    now: datetime | None = None,
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
        rendered = render_digest(
            items,
            label_base_url=label_base_url,
            label_signing_secret=label_signing_secret,
            label_token_lifetime=label_token_lifetime,
            now=now,
        )
        message_id = client.send_email(
            DigestEmail(
                from_address=from_address,
                to_address=to_address,
                subject="modgud digest",
                html_body=rendered.html,
                text_body=rendered.text,
            )
        )
        ItemLog(connection, item_ids[0]).digest_sent(
            item_ids=item_ids,
            postmark_message_id=message_id,
            scheduled_for=scheduled_for,
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
