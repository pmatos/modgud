"""Behavioral tests for selecting the next digest's items."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modgud.config import SecretValue
from modgud.database import connect
from modgud.digests import (
    DigestItem,
    RenderedDigest,
    render_digest,
    select_digest_items,
)
from modgud.formats import ItemFormat
from modgud.span_maps import Span, SpanMap
from modgud.summaries import Tier1Summary


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        yield connection


def _render_digest(items: Sequence[DigestItem]) -> RenderedDigest:
    return render_digest(
        items,
        label_base_url="http://192.168.50.20:8000",
        label_signing_secret=SecretValue(
            "a-dedicated-test-secret-with-at-least-32-bytes"
        ),
        label_token_lifetime=timedelta(days=90),
        now=datetime(2026, 9, 3, 7, tzinfo=UTC),
    )


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


def test_selected_records_include_item_metadata_summary_and_span_map(
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
    event_log.execute("INSERT INTO span_maps (item_id) VALUES (?)", (item_id,))
    event_log.executemany(
        """
        INSERT INTO span_map_spans (
            item_id, position, start_ms, end_ms, description
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (item_id, 0, 65_000, 95_000, "The core tradeoff is introduced."),
            (item_id, 1, 3_725_000, 3_760_000, "The practical result is derived."),
        ],
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
            span_map=SpanMap(
                spans=(
                    Span(
                        start_ms=65_000,
                        end_ms=95_000,
                        description="The core tradeoff is introduced.",
                    ),
                    Span(
                        start_ms=3_725_000,
                        end_ms=3_760_000,
                        description="The practical result is derived.",
                    ),
                )
            ),
        ),
    )


def test_has_transcript_reflects_format_and_stored_transcript(
    event_log: sqlite3.Connection,
) -> None:
    with_transcript = event_log.execute(
        """
        INSERT INTO items (
            canonical_url, content_hash, extracted_text_hash,
            format, state, source
        ) VALUES (?, ?, ?, 'youtube', 'summarized', 'example.com')
        """,
        ("https://www.youtube.com/watch?v=t", "youtube-transcript", "a" * 64),
    ).lastrowid
    without_transcript_hash = event_log.execute(
        """
        INSERT INTO items (canonical_url, content_hash, format, state, source)
        VALUES (?, ?, 'youtube', 'summarized', 'example.com')
        """,
        ("https://www.youtube.com/watch?v=u", "youtube-no-transcript"),
    ).lastrowid
    web_with_hash = event_log.execute(
        """
        INSERT INTO items (
            canonical_url, content_hash, extracted_text_hash,
            format, state, source
        ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
        """,
        ("https://example.com/article", "web-article", "b" * 64),
    ).lastrowid
    assert with_transcript is not None
    assert without_transcript_hash is not None
    assert web_with_hash is not None
    event_log.executemany(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
        [(with_transcript,), (without_transcript_hash,), (web_with_hash,)],
    )

    selected = {item.id: item.has_transcript for item in select_digest_items(event_log)}

    assert selected == {
        with_transcript: True,
        without_transcript_hash: False,
        web_with_hash: False,
    }


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


def test_renders_one_complete_tier_1_item_as_html_and_plain_text() -> None:
    item = DigestItem(
        id=1,
        canonical_url="https://example.com/useful",
        format=ItemFormat.WEB,
        state="summarized",
        source="Example Journal",
        title="A useful article",
        author="Ada Rivera",
        time_to_value_seconds=90,
        summary=Tier1Summary(
            one_liner="An article about choosing work with the fastest payoff.",
            claims=(
                "Short tasks provide feedback quickly.",
                "Feedback reduces uncertainty.",
                "Lower uncertainty improves later choices.",
            ),
        ),
    )

    rendered = _render_digest((item,))

    text_without_label_actions = "\n".join(
        line
        for line in rendered.text.splitlines()
        if "worth it:" not in line.casefold()
    )
    assert (
        text_without_label_actions
        == """modgud digest
=============

1. A useful article
   Example Journal · Ada Rivera · 2 min
   https://example.com/useful

   An article about choosing work with the fastest payoff.

   Claims:
   - Short tasks provide feedback quickly.
   - Feedback reduces uncertainty.
   - Lower uncertainty improves later choices.
""".rstrip()
    )
    assert '<a href="https://example.com/useful">A useful article</a>' in rendered.html
    assert "Example Journal · Ada Rivera · 2 min" in rendered.html
    assert (
        "<p>An article about choosing work with the fastest payoff.</p>"
        in rendered.html
    )
    assert "<li>Short tasks provide feedback quickly.</li>" in rendered.html
    assert "<li>Feedback reduces uncertainty.</li>" in rendered.html
    assert "<li>Lower uncertainty improves later choices.</li>" in rendered.html


@pytest.mark.parametrize("item_format", [ItemFormat.YOUTUBE, ItemFormat.PODCAST])
def test_renders_a_timestamped_span_map_inline_anchored_into_the_transcript(
    item_format: ItemFormat,
) -> None:
    item = DigestItem(
        id=1,
        canonical_url="https://www.youtube.com/watch?v=worthwhile",
        format=item_format,
        state="summarized",
        source="Practical Channel",
        title="A worthwhile conversation",
        author="Ada Rivera",
        time_to_value_seconds=3_760,
        summary=Tier1Summary(
            one_liner="A conversation about making practical tradeoffs.",
            claims=(
                "Fast feedback improves decisions.",
                "Explicit constraints reveal tradeoffs.",
                "Small trials reduce risk.",
            ),
        ),
        span_map=SpanMap(
            spans=(
                Span(
                    start_ms=65_000,
                    end_ms=95_000,
                    description="The core tradeoff is introduced.",
                ),
                Span(
                    start_ms=3_725_000,
                    end_ms=3_760_000,
                    description="The practical result is derived.",
                ),
            )
        ),
        has_transcript=True,
    )

    rendered = _render_digest((item,))

    expected_text = """   - Small trials reduce risk.

   Span map:
   - 01:05–01:35 — The core tradeoff is introduced. — http://192.168.50.20:8000/items/1#t-65000
   - 1:02:05–1:02:40 — The practical result is derived. — http://192.168.50.20:8000/items/1#t-3725000
"""
    assert expected_text in rendered.text
    expected_html = (
        "<li>Small trials reduce risk.</li></ul>"
        "<h3>Span map</h3><ul>"
        '<li><a href="http://192.168.50.20:8000/items/1#t-65000">'
        "01:05–01:35</a> — The core tradeoff is introduced.</li>"
        '<li><a href="http://192.168.50.20:8000/items/1#t-3725000">'
        "1:02:05–1:02:40</a> — The practical result is derived.</li>"
        "</ul>"
    )
    assert expected_html in rendered.html


def test_span_map_timestamps_are_plain_text_without_a_stored_transcript() -> None:
    item = DigestItem(
        id=1,
        canonical_url="https://www.youtube.com/watch?v=worthwhile",
        format=ItemFormat.YOUTUBE,
        state="summarized",
        source="Practical Channel",
        title="A worthwhile conversation",
        author=None,
        time_to_value_seconds=95,
        summary=Tier1Summary(
            one_liner="A conversation about making practical tradeoffs.",
            claims=("Fast feedback improves decisions.",),
        ),
        span_map=SpanMap(
            spans=(
                Span(
                    start_ms=65_000,
                    end_ms=95_000,
                    description="The core tradeoff is introduced.",
                ),
            )
        ),
        has_transcript=False,
    )

    rendered = _render_digest((item,))

    assert "01:05–01:35 — The core tradeoff is introduced.\n" in rendered.text
    assert "<li>01:05–01:35 — The core tradeoff is introduced.</li>" in rendered.html
    assert "Full transcript" not in rendered.text
    assert "Full transcript" not in rendered.html
    assert "#t-" not in rendered.text
    assert "#t-" not in rendered.html


def test_full_transcript_link_appears_for_inline_items_with_a_stored_transcript() -> (
    None
):
    item = DigestItem(
        id=7,
        canonical_url="https://example.com/podcasts/7",
        format=ItemFormat.PODCAST,
        state="summarized",
        source="Practical Podcast",
        title="An episode worth hearing",
        author=None,
        time_to_value_seconds=60,
        summary=Tier1Summary(
            one_liner="An episode about practical tradeoffs.",
            claims=("Fast feedback improves decisions.",),
        ),
        has_transcript=True,
    )

    rendered = _render_digest((item,))

    assert "Full transcript: http://192.168.50.20:8000/items/7" in rendered.text
    assert (
        '<a href="http://192.168.50.20:8000/items/7">Full transcript</a>'
        in rendered.html
    )


def test_full_transcript_link_appears_for_overflow_items_with_a_stored_transcript() -> (
    None
):
    items = tuple(
        DigestItem(
            id=position,
            canonical_url=f"https://example.com/{position}",
            format=ItemFormat.PODCAST if position == 11 else ItemFormat.WEB,
            state="summarized",
            source="example.com",
            title=f"Item {position}",
            author=None,
            time_to_value_seconds=None,
            summary=None,
            has_transcript=position == 11,
        )
        for position in range(1, 12)
    )

    rendered = _render_digest(items)

    assert "Full transcript: http://192.168.50.20:8000/items/11" in rendered.text
    assert (
        '<a href="http://192.168.50.20:8000/items/11">Full transcript</a>'
        in rendered.html
    )
    assert rendered.text.count("Full transcript:") == 1
    assert rendered.html.count("Full transcript</a>") == 1


@pytest.mark.parametrize("state", ["failed", "unsummarizable"])
def test_renders_an_item_without_a_summary_as_capture_only(state: str) -> None:
    item = DigestItem(
        id=1,
        canonical_url="https://example.com/capture",
        format=ItemFormat.PDF,
        state=state,
        source="example.com",
        title="Captured document",
        author=None,
        time_to_value_seconds=None,
        summary=None,
    )

    rendered = _render_digest((item,))

    assert "Capture only — no summary is available." in rendered.text
    assert "Capture only — no summary is available." in rendered.html
    assert "Captured document" in rendered.text
    assert "Captured document" in rendered.html
    assert "https://example.com/capture" in rendered.text
    assert "Claims" not in rendered.text
    assert "<h3>Claims</h3>" not in rendered.html


def test_fifty_items_are_all_rendered_with_only_the_first_ten_expanded() -> None:
    items = tuple(
        DigestItem(
            id=position,
            canonical_url=f"https://example.com/{position}",
            format=ItemFormat.WEB,
            state="summarized",
            source=f"Source {position}",
            title=f"Article {position}",
            author=None,
            time_to_value_seconds=position * 60,
            summary=Tier1Summary(
                one_liner=f"One-line summary {position}.",
                claims=(
                    f"Claim {position} alpha.",
                    f"Claim {position} beta.",
                    f"Claim {position} gamma.",
                ),
            ),
        )
        for position in range(1, 51)
    )

    rendered = _render_digest(items)

    for position in range(1, 51):
        assert f"Article {position}" in rendered.text
        assert f"Article {position}" in rendered.html
        assert f"One-line summary {position}." in rendered.text
        assert f"One-line summary {position}." in rendered.html
    assert "Claim 10 alpha." in rendered.text
    assert "Claim 10 alpha." in rendered.html
    assert "Claim 11 alpha." not in rendered.text
    assert "Claim 11 alpha." not in rendered.html
    assert rendered.text.count("👍 Worth it:") == 50
    assert rendered.text.count("👎 Not worth it:") == 50
    assert rendered.html.count(">👍 Worth it</a>") == 50
    assert rendered.html.count(">👎 Not worth it</a>") == 50
    compact_item = (
        "11. Article 11 — One-line summary 11. — Source 11 — https://example.com/11"
    )
    assert any(line.startswith(compact_item) for line in rendered.text.splitlines())
    assert (
        '<li><a href="https://example.com/50">Article 50</a> — '
        "One-line summary 50. — Source 50<br>"
    ) in rendered.html


def test_compact_capture_only_item_still_says_that_no_summary_is_available() -> None:
    summarized_items = tuple(
        DigestItem(
            id=position,
            canonical_url=f"https://example.com/{position}",
            format=ItemFormat.WEB,
            state="summarized",
            source="example.com",
            title=f"Article {position}",
            author=None,
            time_to_value_seconds=60,
            summary=Tier1Summary(
                one_liner=f"Summary {position}.",
                claims=("First.", "Second.", "Third."),
            ),
        )
        for position in range(1, 11)
    )
    capture_only = DigestItem(
        id=11,
        canonical_url="https://example.com/11",
        format=ItemFormat.PDF,
        state="unsummarizable",
        source="example.com",
        title="Captured PDF",
        author=None,
        time_to_value_seconds=None,
        summary=None,
    )

    rendered = _render_digest((*summarized_items, capture_only))

    expected = (
        "11. Captured PDF — Capture only — no summary is available. — "
        "example.com — https://example.com/11"
    )
    assert any(line.startswith(expected) for line in rendered.text.splitlines())
    assert "Captured PDF</a> — Capture only — no summary is available." in (
        rendered.html
    )


def test_html_escapes_captured_and_generated_content() -> None:
    item = DigestItem(
        id=1,
        canonical_url='https://example.com/?left=1&right="2"',
        format=ItemFormat.WEB,
        state="summarized",
        source="Journal & News",
        title="<A useful article>",
        author='Ada "Ace" Rivera',
        time_to_value_seconds=None,
        summary=Tier1Summary(
            one_liner="Queues <persist> & recover.",
            claims=("One < two.", "Two > one.", "A & B agree."),
        ),
    )

    rendered = _render_digest((item,))

    assert "&lt;A useful article&gt;" in rendered.html
    assert "Journal &amp; News · Ada &quot;Ace&quot; Rivera" in rendered.html
    assert 'href="https://example.com/?left=1&amp;right=&quot;2&quot;"' in rendered.html
    assert "Queues &lt;persist&gt; &amp; recover." in rendered.html
    assert "<li>One &lt; two.</li>" in rendered.html
    assert "<A useful article>" in rendered.text


def test_successful_digest_boundary_discards_compact_overflow(
    event_log: sqlite3.Connection,
) -> None:
    for _position in range(11):
        _add_item(event_log, state="failed")
    selected = select_digest_items(event_log)

    rendered = _render_digest(selected)
    event_log.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'digest_sent', '{}')",
        (selected[0].id,),
    )

    assert "11. example.com — Capture only" in rendered.text
    assert select_digest_items(event_log) == ()
