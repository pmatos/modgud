"""Behavioral tests for the scheduled YouTube audio fallback."""

import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest
from yt_dlp.utils import DownloadError

from modgud.audio_fallbacks import AudioFallbackBatchResult, run_audio_fallback_batch
from modgud.blobs import BlobStore
from modgud.cli import main
from modgud.config import Settings, get_settings
from modgud.database import connect

_AUDIO = b"pretend webm audio"
_TRANSCRIPT = b"""WEBVTT

00:00:01.000 --> 00:00:03.500
The first useful claim.

00:01:02.250 --> 00:01:05.000
The supporting evidence.
"""


class _AudioYoutubeDL:
    options_seen: ClassVar[list[dict[str, Any]]] = []
    downloaded_path: ClassVar[Path | None] = None

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        type(self).options_seen.append(options)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def extract_info(self, url: str, *, download: bool) -> dict[str, str]:
        assert url == "https://www.youtube.com/watch?v=video123"
        assert download is True
        assert self.options["format"] == "bestaudio/best"
        assert self.options["noplaylist"] is True
        audio_path = Path(self.options["paths"]["home"]) / "video123.webm"
        audio_path.write_bytes(_AUDIO)
        type(self).downloaded_path = audio_path
        return {"id": "video123"}


class _OneDownloadFailsYoutubeDL(_AudioYoutubeDL):
    def extract_info(self, url: str, *, download: bool) -> dict[str, str]:
        if url.endswith("video-fails"):
            raise DownloadError("audio stream unavailable")
        return super().extract_info(url, download=download)


class _TranscriptionHandler(BaseHTTPRequestHandler):
    request_body: ClassVar[bytes] = b""
    request_path: ClassVar[str] = ""

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        type(self).request_body = self.rfile.read(content_length)
        type(self).request_path = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt")
        self.send_header("Content-Length", str(len(_TRANSCRIPT)))
        self.end_headers()
        self.wfile.write(_TRANSCRIPT)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _serve_transcription() -> Iterator[tuple[str, type[_TranscriptionHandler]]]:
    class Handler(_TranscriptionHandler):
        pass

    Handler.request_body = b""
    Handler.request_path = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/v1", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _settings(tmp_path: Path, transcription_base_url: str) -> Settings:
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8").replace(
            'base_url = "http://127.0.0.1:8080/v1"',
            f'base_url = "{transcription_base_url}"',
        ),
        encoding="utf-8",
    )
    return get_settings(config_path)


def test_caption_refusal_is_transcribed_in_batch_without_retaining_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    blob_store = BlobStore(tmp_path / "blobs")
    manifest_hash = blob_store.put(b'{"title":"How Durable Queues Work"}')
    with connect(database) as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source
            ) VALUES (?, ?, 'youtube', 'captured', 'Systems Workshop')
            """,
            ("https://www.youtube.com/watch?v=video123", manifest_hash),
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
            (item.lastrowid,),
        )
        connection.execute(
            """
            INSERT INTO events (item_id, type, payload)
            VALUES (?, 'caption_refused', '{"reason":"not a bot"}')
            """,
            (item.lastrowid,),
        )

    _AudioYoutubeDL.options_seen = []
    _AudioYoutubeDL.downloaded_path = None
    monkeypatch.setattr("modgud.youtube.YoutubeDL", _AudioYoutubeDL)
    with _serve_transcription() as (base_url, handler):
        result = run_audio_fallback_batch(
            database,
            blob_store,
            settings=_settings(tmp_path, base_url),
        )

    with connect(database) as connection:
        stored_item = connection.execute(
            "SELECT state, extracted_text_hash FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert result == AudioFallbackBatchResult(attempted=1, transcribed=1, failed=0)
    assert stored_item is not None
    state, transcript_hash = stored_item
    assert state == "extracted"
    assert blob_store.get(transcript_hash) == _TRANSCRIPT
    assert [event[0] for event in events] == [
        "captured",
        "caption_refused",
        "audio_fallback",
        "extracted",
    ]
    assert json.loads(events[-2][1]) == {"outcome": "transcribed"}
    assert json.loads(events[-1][1]) == {
        "extracted_text_hash": transcript_hash,
        "source": "audio_fallback",
    }
    assert len(_AudioYoutubeDL.options_seen) == 1
    assert _AudioYoutubeDL.downloaded_path is not None
    assert not _AudioYoutubeDL.downloaded_path.exists()
    assert handler.request_path == "/v1/audio/transcriptions"
    assert b'name="file"' in handler.request_body
    assert _AUDIO in handler.request_body
    assert b'name="model"' in handler.request_body
    assert b"whisper-1" in handler.request_body
    assert b'name="response_format"' in handler.request_body
    assert b"vtt" in handler.request_body


def test_batch_command_reports_when_no_audio_fallbacks_are_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modgud",
            "--config",
            str(config_path),
            "--data-dir",
            str(tmp_path / "data"),
            "batch",
        ],
    )

    main()

    assert capsys.readouterr().out == (
        "Audio fallback batch: 0 attempted, 0 transcribed, 0 failed\n"
        "Podcast transcript batch: 0 attempted, 0 feed-supplied, "
        "0 transcribed, 0 failed\n"
    )


def test_one_audio_failure_is_recorded_without_aborting_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "modgud.sqlite3"
    blob_store = BlobStore(tmp_path / "blobs")
    with connect(database) as connection:
        for video_id in ("video-fails", "video123"):
            item = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, format, state, source
                ) VALUES (?, ?, 'youtube', 'captured', 'Systems Workshop')
                """,
                (
                    f"https://www.youtube.com/watch?v={video_id}",
                    blob_store.put(video_id.encode()),
                ),
            )
            connection.execute(
                """
                INSERT INTO events (item_id, type, payload)
                VALUES (?, 'captured', '{}')
                """,
                (item.lastrowid,),
            )
            connection.execute(
                """
                INSERT INTO events (item_id, type, payload)
                VALUES (?, 'caption_refused', '{"reason":"not a bot"}')
                """,
                (item.lastrowid,),
            )

    monkeypatch.setattr("modgud.youtube.YoutubeDL", _OneDownloadFailsYoutubeDL)
    with _serve_transcription() as (base_url, _):
        result = run_audio_fallback_batch(
            database,
            blob_store,
            settings=_settings(tmp_path, base_url),
        )

    with connect(database) as connection:
        items = connection.execute(
            "SELECT state, extracted_text_hash FROM items ORDER BY id"
        ).fetchall()
        failed_events = connection.execute(
            """
            SELECT type, payload
            FROM events
            WHERE item_id = 1 AND type IN ('audio_fallback', 'failed')
            ORDER BY id
            """
        ).fetchall()

    assert result == AudioFallbackBatchResult(attempted=2, transcribed=1, failed=1)
    assert items[0] == ("failed", None)
    assert items[1][0] == "extracted"
    assert blob_store.get(items[1][1]) == _TRANSCRIPT
    assert [event[0] for event in failed_events] == ["audio_fallback", "failed"]
    assert json.loads(failed_events[0][1]) == {"outcome": "failed"}
    assert json.loads(failed_events[1][1]) == {
        "error": "audio stream unavailable",
        "stage": "audio_fallback",
    }
