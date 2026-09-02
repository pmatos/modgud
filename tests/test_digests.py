"""Behavioral tests for selecting the next digest's items."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from modgud.database import connect
from modgud.digests import DigestItem, select_digest_items
from modgud.formats import ItemFormat
from modgud.summaries import Tier1Summary


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        yield connection


def _add_item(
    connection: sqlite3.Connection,
    *,
    state: str,
    time_to_value_seconds: int | None = None,
) -> int:
    position = connection.execute("SELECT count(*) FROM items").fetchone()[0]
    cursor = connection.execute(
        """
        INSERT INTO items (
            canonical_url,
            content_hash,
            format,
            state,
            source,
            time_to_value_seconds
        ) VALUES (?, ?, 'web', ?, 'example.com', ?)
        """,
        (
            f"https://example.com/{position}",
            f"content-{position}",
            state,
            time_to_value_seconds,
        ),
    )
    item_id = cursor.lastrowid
    assert item_id is not None
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
        (item_id,),
    )
    return item_id


def test_selects_digest_visible_items_captured_after_the_last_successful_send(
    event_log: sqlite3.Connection,
) -> None:
    already_sent = _add_item(event_log, state="summarized")
    event_log.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'digest_sent', '{}')",
        (already_sent,),
    )
    sent_in_latest_digest = _add_item(event_log, state="summarized")
    event_log.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'digest_sent', '{}')",
        (sent_in_latest_digest,),
    )
    summarized = _add_item(event_log, state="summarized")
    unsummarizable = _add_item(event_log, state="unsummarizable")
    failed = _add_item(event_log, state="failed")
    _add_item(event_log, state="captured")
    _add_item(event_log, state="extracted")

    selected = select_digest_items(event_log)

    assert [item.id for item in selected] == [summarized, unsummarizable, failed]


def test_orders_shortest_time_to_value_first_with_unknown_last(
    event_log: sqlite3.Connection,
) -> None:
    unknown = _add_item(event_log, state="failed")
    longest = _add_item(
        event_log,
        state="summarized",
        time_to_value_seconds=300,
    )
    first_short = _add_item(
        event_log,
        state="unsummarizable",
        time_to_value_seconds=60,
    )
    second_short = _add_item(
        event_log,
        state="summarized",
        time_to_value_seconds=60,
    )

    selected = select_digest_items(event_log)

    assert [item.id for item in selected] == [
        first_short,
        second_short,
        longest,
        unknown,
    ]


def test_selected_records_include_item_metadata_and_the_structured_summary(
    event_log: sqlite3.Connection,
) -> None:
    item_id = _add_item(
        event_log,
        state="summarized",
        time_to_value_seconds=90,
    )
    event_log.execute(
        """
        UPDATE items
        SET title = 'A useful article', author = 'Ada Rivera'
        WHERE id = ?
        """,
        (item_id,),
    )
    event_log.execute(
        """
        INSERT INTO tier_1_summaries (item_id, one_liner, claims)
        VALUES (?, ?, ?)
        """,
        (
            item_id,
            "An article about choosing work with the fastest payoff.",
            json.dumps(
                [
                    "Short tasks provide feedback quickly",
                    "Feedback reduces uncertainty",
                    "Lower uncertainty improves later choices",
                ]
            ),
        ),
    )

    selected = select_digest_items(event_log)

    assert selected == (
        DigestItem(
            id=item_id,
            canonical_url="https://example.com/0",
            format=ItemFormat.WEB,
            state="summarized",
            source="example.com",
            title="A useful article",
            author="Ada Rivera",
            time_to_value_seconds=90,
            summary=Tier1Summary(
                one_liner="An article about choosing work with the fastest payoff.",
                claims=(
                    "Short tasks provide feedback quickly",
                    "Feedback reduces uncertainty",
                    "Lower uncertainty improves later choices",
                ),
            ),
        ),
    )


def test_failed_send_leaves_selection_unchanged_and_selection_writes_nothing(
    event_log: sqlite3.Connection,
) -> None:
    _add_item(event_log, state="summarized", time_to_value_seconds=30)
    _add_item(event_log, state="failed")
    changes_before_selection = event_log.total_changes

    first_attempt = select_digest_items(event_log)
    next_attempt = select_digest_items(event_log)

    assert next_attempt == first_attempt
    assert event_log.total_changes == changes_before_selection


def test_recaptured_item_after_the_send_boundary_is_selected_once(
    event_log: sqlite3.Connection,
) -> None:
    item_id = _add_item(event_log, state="summarized")
    event_log.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'digest_sent', '{}')",
        (item_id,),
    )
    event_log.executemany(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
        [(item_id,), (item_id,)],
    )

    selected = select_digest_items(event_log)

    assert [item.id for item in selected] == [item_id]
