"""The append-only event log: the only writer of the ``events`` table."""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Literal, Protocol

type FallbackOutcome = Literal["failed", "transcribed"]
type TranscriptSource = Literal["audio", "feed"]


class PodcastIdentity(Protocol):
    """The feed coordinates a capture event records for a podcast episode.

    Structural, so that the event log does not depend on the feed parser, and so
    that a capture payload cannot carry a feed URL without its guid.
    """

    @property
    def feed_url(self) -> str: ...

    @property
    def guid(self) -> str: ...


def _encode(fields: Mapping[str, object]) -> str:
    """Render payload fields as the log's one canonical JSON text.

    Non-ASCII is escaped rather than written literally. That is what the
    fourteen-site majority did before this module existed, and it is what keeps
    a lone surrogate — which ``surrogateescape``-decoded fetch input can carry
    into an error message — from raising ``UnicodeEncodeError`` when sqlite
    binds the parameter.
    """
    return json.dumps(dict(fields), separators=(",", ":"), sort_keys=True)


class ItemLog:
    """One item's slice of the event log, bound to one open transaction.

    Construct this inside the ``with connect(...)`` block whose commit will
    carry the events. It never opens a connection and never commits: the
    caller's block owns the transaction.

    Because a connection commits but does not close on leaving its ``with``
    block, an ``ItemLog`` used after that block would write into a transaction
    nobody commits, and the row would be lost without an error. So: never store
    one on a longer-lived object, and never return one from a function.
    """

    __slots__ = ("_connection", "item_id")

    def __init__(self, connection: sqlite3.Connection, item_id: int) -> None:
        self._connection = connection
        self.item_id = item_id

    def captured(
        self,
        *,
        url: str,
        canonical_url: str,
        origin: str | None,
        inbound_message_id: str | None = None,
        fetch_error: str | None = None,
        podcast: PodcastIdentity | None = None,
    ) -> None:
        """Record that ``url`` resolved to this item."""
        # `origin` is written even when None: origin_reports tells a null origin
        # apart from an absent one. The rest are omitted when absent.
        fields: dict[str, object] = {
            "canonical_url": canonical_url,
            "origin": origin,
            "url": url,
        }
        if inbound_message_id is not None:
            fields["inbound_message_id"] = inbound_message_id
        if fetch_error is not None:
            fields["fetch_error"] = fetch_error
        if podcast is not None:
            fields["feed_url"] = podcast.feed_url
            fields["guid"] = podcast.guid
        self._append("captured", fields)

    def extracted(
        self,
        *,
        extracted_text_hash: str,
        source: str | None = None,
        caption_language: str | None = None,
        caption_kind: str | None = None,
    ) -> None:
        """Record that readable text now exists for this item."""
        fields: dict[str, object] = {"extracted_text_hash": extracted_text_hash}
        if source is not None:
            fields["source"] = source
        if caption_language is not None:
            fields["caption_language"] = caption_language
        if caption_kind is not None:
            fields["caption_kind"] = caption_kind
        self._append("extracted", fields)

    def failed(self, *, error: str, stage: str, attempts: int | None = None) -> None:
        """Record that ``stage`` gave up on this item."""
        fields: dict[str, object] = {"error": error, "stage": stage}
        if attempts is not None:
            fields["attempts"] = attempts
        self._append("failed", fields)

    def caption_refused(self, reason: str) -> None:
        """Record that the upstream captions were withheld."""
        self._append("caption_refused", {"reason": reason, "stage": "captions"})

    def unsummarizable(self, reason: str) -> None:
        """Record that this item can never yield a summary."""
        self._append("unsummarizable", {"reason": reason, "stage": "extraction"})

    def summarized(self, *, one_liner: str, claims: Sequence[str], model: str) -> None:
        """Record the tier-1 artifact that was just stored."""
        self._append(
            "summarized",
            {"claims": list(claims), "model": model, "one_liner": one_liner},
        )

    def audio_fallback(self, outcome: FallbackOutcome) -> None:
        """Record the result of an audio transcription attempt."""
        self._append("audio_fallback", {"outcome": outcome})

    def podcast_transcript(
        self, *, source: TranscriptSource, url: str, media_type: str | None = None
    ) -> None:
        """Record which path produced this episode's transcript."""
        fields: dict[str, object] = {"source": source, "url": url}
        if media_type is not None:
            fields["media_type"] = media_type
        self._append("podcast_transcript", fields)

    def digest_sent(
        self,
        *,
        item_ids: Sequence[int],
        postmark_message_id: str,
        scheduled_for: date | None = None,
    ) -> None:
        """Record a delivered digest against its first item."""
        fields: dict[str, object] = {
            "item_ids": list(item_ids),
            "postmark_message_id": postmark_message_id,
        }
        if scheduled_for is not None:
            fields["scheduled_for"] = scheduled_for.isoformat()
        self._append("digest_sent", fields)

    def label(self, label: str) -> None:
        """Record a reader's verdict on this item."""
        self._append("label", {"label": label})

    def _append(self, event_type: str, fields: Mapping[str, object]) -> None:
        """Append one row. The only ``INSERT INTO events`` in the package."""
        self._connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, ?, ?)",
            (self.item_id, event_type, _encode(fields)),
        )
