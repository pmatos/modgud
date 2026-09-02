"""Pure selection of items for the next digest."""

import json
import sqlite3
from dataclasses import dataclass

from modgud.formats import ItemFormat
from modgud.summaries import Tier1Summary


@dataclass(frozen=True, slots=True)
class DigestItem:
    """An item selected for inclusion in the next digest."""

    id: int
    canonical_url: str
    format: ItemFormat
    state: str
    source: str
    title: str | None
    author: str | None
    time_to_value_seconds: int | None
    summary: Tier1Summary | None


def _stored_summary(one_liner: object, claims_json: object) -> Tier1Summary | None:
    if one_liner is None and claims_json is None:
        return None
    if one_liner is None or claims_json is None:
        raise ValueError("stored tier-1 summary must have one-liner and claims")
    claims = json.loads(str(claims_json))
    if not isinstance(claims, list) or any(
        not isinstance(claim, str) for claim in claims
    ):
        raise ValueError("stored tier-1 claims must be an array of strings")
    return Tier1Summary(
        one_liner=str(one_liner),
        claims=tuple(claims),
    )


def select_digest_items(connection: sqlite3.Connection) -> tuple[DigestItem, ...]:
    """Return digest-visible items captured after the last successful send."""
    rows = connection.execute(
        """
        WITH last_success AS (
            SELECT coalesce(max(id), 0) AS event_id
            FROM events
            WHERE type = 'digest_sent'
        ),
        qualifying_captures AS (
            SELECT captures.item_id,
                   min(captures.id) AS first_capture_event_id
            FROM events AS captures
            CROSS JOIN last_success
            WHERE captures.type = 'captured'
              AND captures.id > last_success.event_id
            GROUP BY captures.item_id
        )
        SELECT items.id,
               items.canonical_url,
               items.format,
               items.state,
               items.source,
               items.title,
               items.author,
               items.time_to_value_seconds,
               tier_1_summaries.one_liner,
               tier_1_summaries.claims
        FROM qualifying_captures
        JOIN items ON items.id = qualifying_captures.item_id
        LEFT JOIN tier_1_summaries ON tier_1_summaries.item_id = items.id
        WHERE items.state IN ('summarized', 'unsummarizable', 'failed')
        ORDER BY items.time_to_value_seconds IS NULL,
                 items.time_to_value_seconds,
                 qualifying_captures.first_capture_event_id,
                 items.id
        """
    ).fetchall()
    return tuple(
        DigestItem(
            id=int(row[0]),
            canonical_url=str(row[1]),
            format=ItemFormat(row[2]),
            state=str(row[3]),
            source=str(row[4]),
            title=str(row[5]) if row[5] is not None else None,
            author=str(row[6]) if row[6] is not None else None,
            time_to_value_seconds=int(row[7]) if row[7] is not None else None,
            summary=_stored_summary(row[8], row[9]),
        )
        for row in rows
    )
