"""Existing-item state changes paired with their explanatory events."""

import json
import sqlite3
from collections.abc import Sequence
from typing import Literal

from modgud.events import ItemLog

type ItemState = Literal[
    "captured", "extracted", "summarized", "unsummarizable", "failed"
]


class ItemTransitionConflict(RuntimeError):
    """Raised when an item no longer matches a requested transition."""


def _require_item(updated: sqlite3.Cursor, item_id: int) -> None:
    if updated.rowcount != 1:
        raise ItemTransitionConflict(f"item {item_id} changed during transition")


def mark_extracted(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    extracted_text_hash: str,
    event_source: str | None = None,
    expected_state: ItemState | None = None,
    title: str | None = None,
    author: str | None = None,
    item_source: str | None = None,
) -> None:
    """Store extracted-text facts and append the matching event."""
    if expected_state is None:
        updated = connection.execute(
            """
            UPDATE items
            SET state = 'extracted',
                extracted_text_hash = ?,
                title = coalesce(?, title),
                author = coalesce(?, author),
                source = coalesce(?, source),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (extracted_text_hash, title, author, item_source, item_id),
        )
    else:
        updated = connection.execute(
            """
            UPDATE items
            SET state = 'extracted',
                extracted_text_hash = ?,
                title = coalesce(?, title),
                author = coalesce(?, author),
                source = coalesce(?, source),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
              AND state = ?
              AND extracted_text_hash IS NULL
            """,
            (
                extracted_text_hash,
                title,
                author,
                item_source,
                item_id,
                expected_state,
            ),
        )
    _require_item(updated, item_id)
    ItemLog(connection, item_id).extracted(
        extracted_text_hash=extracted_text_hash,
        source=event_source,
    )


def mark_failed(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    error: str,
    stage: str,
    attempts: int | None = None,
    expected_state: ItemState | None = None,
) -> None:
    """Move an item to failed and append the matching event."""
    if expected_state is None:
        updated = connection.execute(
            """
            UPDATE items
            SET state = 'failed',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (item_id,),
        )
    else:
        updated = connection.execute(
            """
            UPDATE items
            SET state = 'failed',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
              AND state = ?
              AND extracted_text_hash IS NULL
            """,
            (item_id, expected_state),
        )
    _require_item(updated, item_id)
    ItemLog(connection, item_id).failed(
        error=error,
        stage=stage,
        attempts=attempts,
    )


def mark_summary_failed(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    error: str,
    attempts: int,
) -> None:
    """Record a summary failure without hiding an earlier valid summary."""
    updated = connection.execute(
        """
        UPDATE items
        SET state = CASE
                WHEN EXISTS (
                    SELECT 1 FROM tier_1_summaries
                    WHERE tier_1_summaries.item_id = items.id
                ) THEN 'summarized'
                ELSE 'failed'
            END,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (item_id,),
    )
    _require_item(updated, item_id)
    ItemLog(connection, item_id).failed(
        error=error,
        stage="summary",
        attempts=attempts,
    )


def mark_unsummarizable(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    reason: str,
    expected_state: ItemState | None = None,
) -> None:
    """Move an item to unsummarizable and append the matching event."""
    if expected_state is None:
        updated = connection.execute(
            """
            UPDATE items
            SET state = 'unsummarizable',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (item_id,),
        )
    else:
        updated = connection.execute(
            """
            UPDATE items
            SET state = 'unsummarizable',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
              AND state = ?
              AND extracted_text_hash IS NULL
            """,
            (item_id, expected_state),
        )
    _require_item(updated, item_id)
    ItemLog(connection, item_id).unsummarizable(reason)


def mark_summarized(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    one_liner: str,
    claims: Sequence[str],
    model: str,
) -> None:
    """Store a tier-1 artifact and append the matching state event."""
    stored_claims = json.dumps(list(claims), ensure_ascii=False)
    connection.execute(
        """
        INSERT INTO tier_1_summaries (item_id, one_liner, claims)
        VALUES (?, ?, ?)
        ON CONFLICT (item_id) DO UPDATE SET
            one_liner = excluded.one_liner,
            claims = excluded.claims,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (item_id, one_liner, stored_claims),
    )
    updated = connection.execute(
        """
        UPDATE items
        SET state = 'summarized',
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (item_id,),
    )
    _require_item(updated, item_id)
    ItemLog(connection, item_id).summarized(
        one_liner=one_liner,
        claims=claims,
        model=model,
    )
