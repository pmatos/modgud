"""Behavioral tests for the append-only event log."""

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from modgud.database import connect
from modgud.events import ItemLog


def _item(
    connection: sqlite3.Connection, canonical_url: str = "https://a.test/x"
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO items (canonical_url, content_hash, format, state, source)
        VALUES (?, ?, 'web', 'captured', 'a.test')
        """,
        (canonical_url, f"{abs(hash(canonical_url)):064x}"[:64]),
    )
    item_id = cursor.lastrowid
    assert item_id is not None
    return item_id


def _rows(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (str(row[0]), str(row[1]))
        for row in connection.execute("SELECT type, payload FROM events ORDER BY id")
    ]


def _only(connection: sqlite3.Connection) -> tuple[str, str]:
    rows = _rows(connection)
    assert len(rows) == 1
    return rows[0]


# Every payload shape the six writer modules produce today. Each case is
# (label, call, expected type, expected payload text). The payload text is the
# exact string the pre-existing hand-written writer stored, so this table is the
# byte-compatibility contract for all sixteen replaced call sites.
_SHAPES: list[tuple[str, Callable[[ItemLog], None], str, str]] = [
    (
        "captured-minimal",
        lambda log: log.captured(
            url="https://a.test/x", canonical_url="https://a.test/x", origin=None
        ),
        "captured",
        '{"canonical_url":"https://a.test/x","origin":null,"url":"https://a.test/x"}',
    ),
    (
        "captured-inbound-and-fetch-error",
        lambda log: log.captured(
            url="https://a.test/x",
            canonical_url="https://a.test/x",
            origin="mail",
            inbound_message_id="m-1",
            fetch_error="boom",
        ),
        "captured",
        (
            '{"canonical_url":"https://a.test/x","fetch_error":"boom",'
            '"inbound_message_id":"m-1","origin":"mail","url":"https://a.test/x"}'
        ),
    ),
    (
        "extracted-plain",
        lambda log: log.extracted(extracted_text_hash="d" * 64),
        "extracted",
        '{"extracted_text_hash":"%s"}' % ("d" * 64),
    ),
    (
        "extracted-captions",
        lambda log: log.extracted(
            extracted_text_hash="d" * 64, caption_language="en", caption_kind="manual"
        ),
        "extracted",
        '{"caption_kind":"manual","caption_language":"en",'
        '"extracted_text_hash":"%s"}' % ("d" * 64),
    ),
    (
        "extracted-with-source",
        lambda log: log.extracted(
            extracted_text_hash="d" * 64, source="audio_fallback"
        ),
        "extracted",
        '{"extracted_text_hash":"%s","source":"audio_fallback"}' % ("d" * 64),
    ),
    (
        "failed",
        lambda log: log.failed(error="nope", stage="extraction"),
        "failed",
        '{"error":"nope","stage":"extraction"}',
    ),
    (
        "failed-with-attempts",
        lambda log: log.failed(
            error="model returned malformed summary output",
            stage="summary",
            attempts=2,
        ),
        "failed",
        (
            '{"attempts":2,"error":"model returned malformed summary output",'
            '"stage":"summary"}'
        ),
    ),
    (
        "caption-refused",
        lambda log: log.caption_refused("age restricted"),
        "caption_refused",
        '{"reason":"age restricted","stage":"captions"}',
    ),
    (
        "unsummarizable",
        lambda log: log.unsummarizable("no readable text"),
        "unsummarizable",
        '{"reason":"no readable text","stage":"extraction"}',
    ),
    (
        "summarized",
        lambda log: log.summarized(
            one_liner="A thing happened.", claims=("one", "two"), model="m-1"
        ),
        "summarized",
        '{"claims":["one","two"],"model":"m-1","one_liner":"A thing happened."}',
    ),
    (
        "audio-fallback-transcribed",
        lambda log: log.audio_fallback("transcribed"),
        "audio_fallback",
        '{"outcome":"transcribed"}',
    ),
    (
        "podcast-transcript-feed",
        lambda log: log.podcast_transcript(
            source="feed", url="https://a.test/t.vtt", media_type="text/vtt"
        ),
        "podcast_transcript",
        '{"media_type":"text/vtt","source":"feed","url":"https://a.test/t.vtt"}',
    ),
    (
        "podcast-transcript-audio",
        lambda log: log.podcast_transcript(source="audio", url="https://a.test/a.mp3"),
        "podcast_transcript",
        '{"source":"audio","url":"https://a.test/a.mp3"}',
    ),
    (
        "digest-sent",
        lambda log: log.digest_sent(item_ids=(7, 9), postmark_message_id="pm-1"),
        "digest_sent",
        '{"item_ids":[7,9],"postmark_message_id":"pm-1"}',
    ),
    (
        "digest-sent-scheduled",
        lambda log: log.digest_sent(
            item_ids=(7,), postmark_message_id="pm-1", scheduled_for=date(2026, 9, 20)
        ),
        "digest_sent",
        '{"item_ids":[7],"postmark_message_id":"pm-1","scheduled_for":"2026-09-20"}',
    ),
    (
        "label",
        lambda log: log.label("worth-it"),
        "label",
        '{"label":"worth-it"}',
    ),
]


@pytest.mark.parametrize(
    ("call", "event_type", "payload"),
    [pytest.param(c, t, p, id=name) for name, c, t, p in _SHAPES],
)
def test_each_event_kind_stores_the_payload_its_writers_stored_before(
    tmp_path: Path,
    call: Callable[[ItemLog], None],
    event_type: str,
    payload: str,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        call(ItemLog(connection, _item(connection)))

        assert _only(connection) == (event_type, payload)


def test_an_absent_optional_field_is_omitted_rather_than_stored_as_null(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).captured(
            url="https://a.test/x",
            canonical_url="https://a.test/x",
            origin=None,
            inbound_message_id=None,
            fetch_error=None,
        )

        _, payload = _only(connection)

    # `origin` is always written, including as null, because origin_reports
    # distinguishes a null origin from an absent one via json_type. The other
    # three optional fields disappear entirely.
    assert set(json.loads(payload)) == {"canonical_url", "origin", "url"}
    assert json.loads(payload)["origin"] is None


def test_a_podcast_capture_records_the_feed_and_guid_together(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).captured(
            url="https://a.test/ep",
            canonical_url="https://a.test/ep",
            origin=None,
            podcast=_Podcast(feed_url="https://a.test/feed.xml", guid="g-1"),
        )

        _, payload = _only(connection)

    assert json.loads(payload)["feed_url"] == "https://a.test/feed.xml"
    assert json.loads(payload)["guid"] == "g-1"


class _Podcast:
    """A stand-in for PodcastEpisode, to show the log needs only the two fields."""

    def __init__(self, *, feed_url: str, guid: str) -> None:
        self.feed_url = feed_url
        self.guid = guid


def test_payloads_are_stored_compactly_with_their_keys_sorted(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).podcast_transcript(
            source="feed", url="https://a.test/t.vtt", media_type="text/vtt"
        )

        _, payload = _only(connection)

    assert " " not in payload
    assert (
        payload.index('"media_type"')
        < payload.index('"source"')
        < payload.index('"url"')
    )


def test_non_ascii_payload_text_is_escaped_but_still_reads_back_intact(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).failed(error="café", stage="extraction")

        _, payload = _only(connection)
        extracted = connection.execute(
            "SELECT json_extract(payload, '$.error') FROM events"
        ).fetchone()[0]

    assert "caf\\u00e9" in payload
    assert json.loads(payload)["error"] == "café"
    assert extracted == "café"


def test_a_lone_surrogate_in_an_error_message_can_still_be_recorded(
    tmp_path: Path,
) -> None:
    # cli decodes fetched bytes with surrogateescape, so a lone surrogate can
    # reach an error message. Escaping is what keeps sqlite binding from raising
    # UnicodeEncodeError, which is what ensure_ascii=False would do here.
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).failed(
            error="\udc80bad", stage="extraction"
        )

        _, payload = _only(connection)

    assert json.loads(payload)["error"] == "\udc80bad"


def test_the_log_writes_into_the_transaction_its_caller_already_opened(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "modgud.sqlite3")
    try:
        item_id = _item(connection)
        ItemLog(connection, item_id).label("worth-it")

        # Visible on the caller's own connection before any commit, so the log
        # did not open a connection of its own...
        assert len(_rows(connection)) == 1

        connection.rollback()

        # ...and it did not commit, so the caller's rollback takes the event with it.
        assert _rows(connection) == []
    finally:
        connection.close()


def test_events_are_appended_in_call_order(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        log = ItemLog(connection, _item(connection))
        log.audio_fallback("failed")
        log.failed(error="boom", stage="audio_fallback")

        assert [event_type for event_type, _ in _rows(connection)] == [
            "audio_fallback",
            "failed",
        ]


def test_two_logs_on_one_connection_record_against_their_own_items(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        first = _item(connection, "https://a.test/one")
        second = _item(connection, "https://a.test/two")
        ItemLog(connection, first).label("worth-it")
        ItemLog(connection, second).label("not-worth-it")

        recorded = connection.execute(
            "SELECT item_id, json_extract(payload, '$.label') FROM events ORDER BY id"
        ).fetchall()

    assert recorded == [(first, "worth-it"), (second, "not-worth-it")]


def test_the_database_supplies_the_timestamp(tmp_path: Path) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).label("worth-it")

        created_at = connection.execute("SELECT created_at FROM events").fetchone()[0]

    assert created_at is not None


def test_recording_against_an_unknown_item_is_refused(tmp_path: Path) -> None:
    with (
        connect(tmp_path / "modgud.sqlite3") as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        ItemLog(connection, 9999).label("worth-it")


def test_an_event_written_through_the_log_still_cannot_be_updated(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        ItemLog(connection, _item(connection)).label("worth-it")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE events SET type = 'label2'")
