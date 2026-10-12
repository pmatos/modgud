"""Behavior of signed Label confirmation and recording through one interface."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modgud.config import SecretValue
from modgud.database import connect
from modgud.label_tokens import create_label_token
from modgud.labels import LabelFailure, LabelLinks, LabelRejected

_NOW = datetime(2026, 10, 13, tzinfo=UTC)
_SECRET = SecretValue("a-dedicated-test-secret-with-at-least-32-bytes")


def _token(
    item_id: int = 1,
    opinion: str = "worth-it",
    *,
    expires_at: datetime = _NOW + timedelta(days=1),
) -> str:
    return create_label_token(
        item_id, opinion, signing_secret=_SECRET, expires_at=expires_at
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "modgud.sqlite3"
    with connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO items (
                id, canonical_url, content_hash, format, state, source, title, page_url
            ) VALUES (?, ?, ?, 'web', 'captured', 'example.com', ?, ?)
            """,
            [
                (
                    1,
                    "https://example.com/1",
                    "1",
                    "An article",
                    "https://example.com/page",
                ),
                (2, "https://example.com/2", "2", None, "https://example.com/episode"),
                (3, "https://example.com/3", "3", None, None),
            ],
        )
    return path


@pytest.mark.parametrize(
    ("item_id", "title", "url"),
    [
        (1, "An article", "https://example.com/page"),
        (2, "https://example.com/episode", "https://example.com/episode"),
        (3, "https://example.com/3", "https://example.com/3"),
    ],
)
def test_confirmation_resolves_display_fields_without_appending(
    database: Path, item_id: int, title: str, url: str
) -> None:
    links = LabelLinks(database, signing_secret=_SECRET, clock=lambda: _NOW)

    item = links.confirm(item_id, "worth-it", _token(item_id))

    assert (item.item_id, item.title, item.url, item.opinion, item.opinion_name) == (
        item_id,
        title,
        url,
        "worth-it",
        "Worth it",
    )
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_recording_appends_every_opinion_and_preserves_unlabelled_items(
    database: Path,
) -> None:
    links = LabelLinks(database, signing_secret=_SECRET, clock=lambda: _NOW)

    first = links.record(1, "worth-it", _token())
    repeated = links.record(1, "worth-it", _token())
    changed = links.record(1, "not-worth-it", _token(opinion="not-worth-it"))

    assert first == repeated
    assert (changed.opinion, changed.opinion_name) == ("not-worth-it", "Not worth it")
    with connect(database) as connection:
        assert connection.execute(
            "SELECT item_id, type, json_extract(payload, '$.label') "
            "FROM events ORDER BY id"
        ).fetchall() == [
            (1, "label", "worth-it"),
            (1, "label", "worth-it"),
            (1, "label", "not-worth-it"),
        ]


@pytest.mark.parametrize("operation", ["confirm", "record"])
@pytest.mark.parametrize(
    ("item_id", "opinion", "token", "failure"),
    [
        (404, "maybe", None, LabelFailure.UNKNOWN_OPINION),
        (404, "worth-it", None, LabelFailure.INVALID_LINK),
        (404, "worth-it", "malformed", LabelFailure.INVALID_LINK),
        (404, "worth-it", _token(), LabelFailure.WRONG_ITEM),
        (1, "not-worth-it", _token(), LabelFailure.WRONG_OPINION),
        (1, "worth-it", _token(expires_at=_NOW), LabelFailure.EXPIRED_LINK),
        # Scope errors precede expiry, and expiry precedes Item existence.
        (404, "worth-it", _token(expires_at=_NOW), LabelFailure.WRONG_ITEM),
        (1, "not-worth-it", _token(expires_at=_NOW), LabelFailure.WRONG_OPINION),
        (404, "worth-it", _token(404, expires_at=_NOW), LabelFailure.EXPIRED_LINK),
        (404, "worth-it", _token(404), LabelFailure.MISSING_ITEM),
        (1, "worth-it", _token().split(".")[0] + ".AAAA", LabelFailure.INVALID_LINK),
    ],
)
def test_rejections_preserve_acceptance_order_and_never_append(
    database: Path,
    operation: str,
    item_id: int,
    opinion: str,
    token: str | None,
    failure: LabelFailure,
) -> None:
    links = LabelLinks(database, signing_secret=_SECRET, clock=lambda: _NOW)
    accept = links.confirm if operation == "confirm" else links.record

    with pytest.raises(LabelRejected) as rejected:
        accept(item_id, opinion, token)

    assert rejected.value.reason is failure
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["confirm", "record"])
@pytest.mark.parametrize("opinion", ["worth-it", "maybe"])
def test_missing_secret_and_unknown_opinion_do_not_consult_clock_or_database(
    tmp_path: Path, operation: str, opinion: str
) -> None:
    database = tmp_path / "not-created.sqlite3"

    def unexpected_clock() -> datetime:
        raise AssertionError("No clock is needed before a complete capability")

    links = LabelLinks(database, signing_secret=None, clock=unexpected_clock)
    accept = links.confirm if operation == "confirm" else links.record

    with pytest.raises(LabelRejected) as rejected:
        accept(404, opinion, _token(404))

    expected = (
        LabelFailure.UNKNOWN_OPINION
        if opinion == "maybe"
        else LabelFailure.INVALID_LINK
    )
    assert rejected.value.reason is expected
    assert not database.exists()


def test_rejected_capability_does_not_open_database(tmp_path: Path) -> None:
    database = tmp_path / "not-created.sqlite3"
    links = LabelLinks(database, signing_secret=_SECRET, clock=lambda: _NOW)

    with pytest.raises(LabelRejected) as rejected:
        links.record(404, "worth-it", "malformed")

    assert rejected.value.reason is LabelFailure.INVALID_LINK
    assert not database.exists()


def test_storage_failure_propagates_without_committing_a_label(database: Path) -> None:
    with connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_label AFTER INSERT ON events
            WHEN NEW.type = 'label'
            BEGIN
                SELECT RAISE(ABORT, 'label write failed');
            END
            """
        )
    links = LabelLinks(database, signing_secret=_SECRET, clock=lambda: _NOW)

    with pytest.raises(sqlite3.IntegrityError, match="label write failed"):
        links.record(1, "worth-it", _token())

    with connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
