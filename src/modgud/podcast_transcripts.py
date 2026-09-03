"""Creator-transcript normalization and podcast audio fallback."""

import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from http.client import HTTPException
from math import isfinite
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from openai import OpenAIError

from modgud.blobs import BlobStore
from modgud.config import Settings
from modgud.database import connect
from modgud.models import RoutedModelClient, create_model_client
from modgud.podcasts import (
    PodcastFeedError,
    PodcastTranscript,
    parse_podcast_episode_resources,
)
from modgud.transcripts import chunk_transcript

_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/srt",
        "application/x-subrip",
        "text/html",
        "text/plain",
        "text/srt",
        "text/vtt",
    }
)
_SRT_TIMING_LINE = re.compile(
    r"^(?P<start>\d{2,}:\d{2}:\d{2}),(?P<start_ms>\d{3})"
    r"(?P<arrow>\s+-->\s+)"
    r"(?P<end>\d{2,}:\d{2}:\d{2}),(?P<end_ms>\d{3})(?P<settings>.*)$"
)


class PodcastTranscriptError(ValueError):
    """A creator transcript does not match its declared supported format."""


@dataclass(frozen=True, slots=True)
class PodcastTranscriptBatchResult:
    """Counts produced by one podcast-transcript batch."""

    attempted: int
    feed_supplied: int
    transcribed: int
    failed: int


@dataclass(frozen=True, slots=True)
class _Cue:
    start_ms: int
    end_ms: int
    text: str


def normalize_podcast_transcript(
    content: bytes,
    *,
    media_type: str,
    duration_seconds: float | None,
) -> bytes:
    """Normalize one supported Podcast Namespace transcript to WebVTT."""
    normalized_type = media_type.partition(";")[0].strip().lower()
    if normalized_type not in _SUPPORTED_MEDIA_TYPES:
        raise PodcastTranscriptError(f"unsupported transcript type: {media_type}")
    if normalized_type == "text/vtt":
        try:
            chunks = chunk_transcript(content)
        except UnicodeDecodeError as error:
            raise PodcastTranscriptError("WebVTT transcript is not UTF-8") from error
        if not chunks:
            raise PodcastTranscriptError("WebVTT transcript has no usable cues")
        return content
    if normalized_type in {
        "application/srt",
        "application/x-subrip",
        "text/srt",
    }:
        normalized = _normalize_srt(content)
    elif normalized_type == "application/json":
        normalized = _normalize_json(content, duration_seconds=duration_seconds)
    elif normalized_type == "text/html":
        normalized = _normalize_html(content, duration_seconds=duration_seconds)
    else:
        normalized = _normalize_plain_text(
            content,
            duration_seconds=duration_seconds,
        )
    if not chunk_transcript(normalized):
        raise PodcastTranscriptError("transcript has no usable cues")
    return normalized


def run_podcast_transcript_batch(
    database: str | Path,
    blob_store: BlobStore,
    *,
    settings: Settings,
) -> PodcastTranscriptBatchResult:
    """Extract every captured podcast through its creator transcript first."""
    with connect(database) as connection:
        pending = connection.execute(
            """
            SELECT id, content_hash, duration_seconds
            FROM items
            WHERE format = 'podcast'
              AND state = 'captured'
              AND extracted_text_hash IS NULL
            ORDER BY id
            """
        ).fetchall()

    feed_supplied = 0
    transcribed = 0
    failed = 0
    routed: RoutedModelClient | None = None
    try:
        for item_id, content_hash, duration_seconds in pending:
            transcript = None
            path_fields: dict[str, str] | None = None
            error_message = "episode has neither a usable transcript nor audio"
            try:
                resources = parse_podcast_episode_resources(
                    blob_store.get(str(content_hash))
                )
            except (OSError, PodcastFeedError) as error:
                resources = None
                error_message = str(error)
            if resources is not None:
                for candidate in _preferred_transcripts(resources.transcripts):
                    try:
                        transcript = normalize_podcast_transcript(
                            _fetch(candidate.url),
                            media_type=candidate.media_type,
                            duration_seconds=duration_seconds,
                        )
                    except (HTTPException, OSError, PodcastTranscriptError) as error:
                        error_message = str(error)
                        continue
                    path_fields = {
                        "media_type": candidate.media_type,
                        "source": "feed",
                        "url": candidate.url,
                    }
                    break
            if transcript is None and resources is not None and resources.audio_url:
                try:
                    if routed is None:
                        routed = create_model_client("transcription", settings=settings)
                    with (
                        _download_audio(resources.audio_url) as audio_path,
                        audio_path.open("rb") as audio,
                    ):
                        response = routed.client.audio.transcriptions.create(
                            file=audio,
                            model=routed.model,
                            response_format="vtt",
                        )
                    transcript = response.encode("utf-8")
                    if not chunk_transcript(transcript):
                        raise PodcastTranscriptError(
                            "audio transcription returned no usable cues"
                        )
                except (
                    HTTPException,
                    OpenAIError,
                    OSError,
                    PodcastTranscriptError,
                    ValueError,
                ) as error:
                    error_message = str(error)
                else:
                    path_fields = {
                        "source": "audio",
                        "url": resources.audio_url,
                    }
            if transcript is None or path_fields is None:
                _record_failure(database, int(item_id), error_message)
                failed += 1
                continue

            transcript_hash = blob_store.put(transcript)
            path_payload = json.dumps(
                path_fields,
                separators=(",", ":"),
                sort_keys=True,
            )
            extraction_payload = json.dumps(
                {
                    "extracted_text_hash": transcript_hash,
                    "source": (
                        "podcast_feed"
                        if path_fields["source"] == "feed"
                        else "audio_fallback"
                    ),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            with connect(database) as connection:
                connection.execute(
                    """
                    UPDATE items
                    SET extracted_text_hash = ?,
                        state = 'extracted',
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id = ?
                    """,
                    (transcript_hash, item_id),
                )
                connection.execute(
                    """
                    INSERT INTO events (item_id, type, payload)
                    VALUES (?, 'podcast_transcript', ?)
                    """,
                    (item_id, path_payload),
                )
                connection.execute(
                    """
                    INSERT INTO events (item_id, type, payload)
                    VALUES (?, 'extracted', ?)
                    """,
                    (item_id, extraction_payload),
                )
            if path_fields["source"] == "feed":
                feed_supplied += 1
            else:
                transcribed += 1
    finally:
        if routed is not None:
            routed.client.close()

    return PodcastTranscriptBatchResult(
        attempted=len(pending),
        feed_supplied=feed_supplied,
        transcribed=transcribed,
        failed=failed,
    )


def _preferred_transcripts(
    transcripts: tuple[PodcastTranscript, ...],
) -> tuple[PodcastTranscript, ...]:
    priority = {
        "text/vtt": 0,
        "application/x-subrip": 1,
        "application/srt": 1,
        "text/srt": 1,
        "application/json": 2,
        "text/html": 3,
        "text/plain": 4,
    }
    supported = (
        transcript
        for transcript in transcripts
        if transcript.media_type in _SUPPORTED_MEDIA_TYPES
    )
    return tuple(sorted(supported, key=lambda value: priority[value.media_type]))


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "modgud/0.1"})
    with urlopen(request) as response:
        return cast("bytes", response.read())


@contextmanager
def _download_audio(url: str) -> Iterator[Path]:
    suffix = Path(urlsplit(url).path).suffix
    if len(suffix) > 16:
        suffix = ""
    with tempfile.TemporaryDirectory(prefix="modgud-podcast-") as directory:
        path = Path(directory) / f"episode{suffix}"
        request = Request(url, headers={"User-Agent": "modgud/0.1"})
        with urlopen(request) as response, path.open("wb") as audio:
            shutil.copyfileobj(response, audio)
        yield path


def _record_failure(database: str | Path, item_id: int, error: str) -> None:
    failure_payload = json.dumps(
        {"error": error, "stage": "podcast_transcript"},
        separators=(",", ":"),
        sort_keys=True,
    )
    with connect(database) as connection:
        connection.execute(
            """
            UPDATE items
            SET state = 'failed',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (item_id,),
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'failed', ?)",
            (item_id, failure_payload),
        )


def _normalize_srt(content: bytes) -> bytes:
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise PodcastTranscriptError("SRT transcript is not UTF-8") from error
    converted = ["WEBVTT", ""]
    for line in lines:
        timing = _SRT_TIMING_LINE.fullmatch(line.strip())
        if timing is None:
            converted.append(line)
            continue
        converted.append(
            f"{timing['start']}.{timing['start_ms']}"
            f"{timing['arrow']}"
            f"{timing['end']}.{timing['end_ms']}"
            f"{timing['settings']}"
        )
    return ("\n".join(converted).rstrip() + "\n").encode()


def _normalize_json(content: bytes, *, duration_seconds: float | None) -> bytes:
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PodcastTranscriptError("transcript is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise PodcastTranscriptError("JSON transcript must be an object")
    segments = cast("dict[str, Any]", parsed).get("segments")
    if not isinstance(segments, list) or not segments:
        raise PodcastTranscriptError("JSON transcript has no segments")

    starts: list[int] = []
    ends: list[int | None] = []
    bodies: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise PodcastTranscriptError("JSON transcript segment must be an object")
        fields = cast("dict[str, Any]", segment)
        body = fields.get("body")
        if not isinstance(body, str) or not body.strip():
            raise PodcastTranscriptError("JSON transcript segment has no body")
        starts.append(_milliseconds(fields.get("startTime"), field="startTime"))
        end_time = fields.get("endTime")
        ends.append(
            None if end_time is None else _milliseconds(end_time, field="endTime")
        )
        bodies.append(body.strip())

    cues = []
    duration_ms = _duration_ms(duration_seconds)
    for index, (start_ms, end_ms, body) in enumerate(zip(starts, ends, bodies)):
        if end_ms is None:
            if index + 1 < len(starts):
                end_ms = starts[index + 1]
            else:
                end_ms = duration_ms if duration_ms is not None else start_ms
        if end_ms < start_ms:
            raise PodcastTranscriptError("JSON transcript cue ends before it starts")
        cues.append(_Cue(start_ms=start_ms, end_ms=end_ms, text=body))
    return _webvtt(cues)


class _TranscriptHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[int | None, str]] = []
        self._paragraph_depth = 0
        self._paragraph_parts: list[str] = []
        self._time_depth = 0
        self._time_parts: list[str] = []
        self._pending_time: int | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag == "p":
            self._paragraph_depth = 1
            self._paragraph_parts = []
        elif self._paragraph_depth:
            self._paragraph_depth += 1
        if tag == "time":
            self._time_depth = 1
            self._time_parts = []
            fields = dict(attrs)
            if fields.get("datetime"):
                self._pending_time = _time_text_ms(cast("str", fields["datetime"]))
        elif self._time_depth:
            self._time_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._paragraph_depth:
            self._paragraph_depth -= 1
            if tag == "p" and self._paragraph_depth == 0:
                text = " ".join("".join(self._paragraph_parts).split())
                if text:
                    self.paragraphs.append((self._pending_time, text))
                self._pending_time = None
        if self._time_depth:
            self._time_depth -= 1
            if tag == "time" and self._time_depth == 0:
                text = "".join(self._time_parts).strip()
                if text:
                    self._pending_time = _time_text_ms(text)

    def handle_data(self, data: str) -> None:
        if self._paragraph_depth:
            self._paragraph_parts.append(data)
        if self._time_depth:
            self._time_parts.append(data)


def _normalize_html(content: bytes, *, duration_seconds: float | None) -> bytes:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PodcastTranscriptError("HTML transcript is not UTF-8") from error
    parser = _TranscriptHTMLParser()
    try:
        parser.feed(decoded)
        parser.close()
    except ValueError as error:
        raise PodcastTranscriptError("HTML transcript has an invalid time") from error
    if not parser.paragraphs:
        raise PodcastTranscriptError("HTML transcript has no paragraphs")

    duration_ms = _duration_ms(duration_seconds)
    if all(start is None for start, _ in parser.paragraphs):
        text = "\n".join(text for _, text in parser.paragraphs)
        return _webvtt([_Cue(0, duration_ms or 0, text)])

    cues = []
    for index, (start, text) in enumerate(parser.paragraphs):
        start_ms = start if start is not None else 0
        next_starts = [
            following
            for following, _ in parser.paragraphs[index + 1 :]
            if following is not None
        ]
        end_ms = next_starts[0] if next_starts else duration_ms or start_ms
        if end_ms < start_ms:
            raise PodcastTranscriptError("HTML transcript times are out of order")
        cues.append(_Cue(start_ms, end_ms, text))
    return _webvtt(cues)


def _normalize_plain_text(
    content: bytes,
    *,
    duration_seconds: float | None,
) -> bytes:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PodcastTranscriptError("plain-text transcript is not UTF-8") from error
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        raise PodcastTranscriptError("plain-text transcript is empty")
    return _webvtt([_Cue(0, _duration_ms(duration_seconds) or 0, text)])


def _milliseconds(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PodcastTranscriptError(f"JSON transcript {field} must be a number")
    if not isfinite(value) or value < 0:
        raise PodcastTranscriptError(
            f"JSON transcript {field} must be finite and non-negative"
        )
    return round(value * 1_000)


def _duration_ms(duration_seconds: float | None) -> int | None:
    if duration_seconds is None:
        return None
    if not isfinite(duration_seconds) or duration_seconds < 0:
        return None
    return round(duration_seconds * 1_000)


def _time_text_ms(value: str) -> int:
    fields = value.strip().split(":")
    try:
        if len(fields) == 1:
            seconds = float(fields[0])
        elif len(fields) == 2:
            seconds = int(fields[0]) * 60 + float(fields[1])
        elif len(fields) == 3:
            seconds = int(fields[0]) * 3_600 + int(fields[1]) * 60 + float(fields[2])
        else:
            raise ValueError
    except ValueError as error:
        raise ValueError("invalid transcript time") from error
    if not isfinite(seconds) or seconds < 0:
        raise ValueError("invalid transcript time")
    return round(seconds * 1_000)


def _webvtt(cues: list[_Cue]) -> bytes:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.extend(
            (
                f"{_vtt_timestamp(cue.start_ms)} --> {_vtt_timestamp(cue.end_ms)}",
                escape(cue.text, quote=False),
                "",
            )
        )
    return "\n".join(lines).encode()


def _vtt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"
