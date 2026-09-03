"""Behavioral tests for podcast creator transcripts and audio fallback."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from modgud.blobs import BlobStore
from modgud.config import Settings, get_settings
from modgud.database import connect
from modgud.podcast_transcripts import (
    PodcastTranscriptBatchResult,
    normalize_podcast_transcript,
    run_podcast_transcript_batch,
)
from modgud.podcasts import PodcastTranscript, parse_podcast_feed
from modgud.transcripts import chunk_transcript


class _PodcastResourceHandler(BaseHTTPRequestHandler):
    post_body: ClassVar[bytes] = b""
    post_path: ClassVar[str] = ""
    request_counts: ClassVar[dict[str, int]] = {}
    routes: ClassVar[dict[str, tuple[bytes, str]]] = {}
    transcription: ClassVar[bytes | None] = None

    def do_GET(self) -> None:
        type(self).request_counts[self.path] = (
            type(self).request_counts.get(self.path, 0) + 1
        )
        response = type(self).routes.get(self.path)
        if response is None:
            self.send_error(404)
            return
        body, content_type = response
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        type(self).post_path = self.path
        type(self).post_body = self.rfile.read(int(self.headers["Content-Length"]))
        body = type(self).transcription
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _serve_resources(
    routes: dict[str, tuple[bytes, str]],
    *,
    transcription: bytes | None = None,
) -> Iterator[tuple[str, type[_PodcastResourceHandler]]]:
    class Handler(_PodcastResourceHandler):
        pass

    Handler.request_counts = {}
    Handler.routes = routes
    Handler.post_body = b""
    Handler.post_path = ""
    Handler.transcription = transcription
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", Handler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _settings(tmp_path: Path, transcription_base_url: str | None = None) -> Settings:
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "config.toml"
    content = example.read_text(encoding="utf-8")
    if transcription_base_url is not None:
        content = content.replace(
            'base_url = "http://127.0.0.1:8080/v1"',
            f'base_url = "{transcription_base_url}"',
        )
    config_path.write_text(content, encoding="utf-8")
    return get_settings(config_path)


def _store_episode(
    tmp_path: Path,
    *,
    feed: bytes,
    feed_url: str,
) -> BlobStore:
    episode = parse_podcast_feed(feed, feed_url=feed_url)
    blob_store = BlobStore(tmp_path / "blobs")
    manifest_hash = blob_store.put(episode.raw_content)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, format, state, source,
                duration_seconds
            ) VALUES (?, ?, 'podcast', 'captured', 'A Test Podcast', ?)
            """,
            (episode.canonical_url, manifest_hash, episode.duration_seconds),
        )
        connection.execute(
            "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', '{}')",
            (item.lastrowid,),
        )
    return blob_store


def test_selected_episode_exposes_creator_transcripts_and_audio() -> None:
    feed = b"""<?xml version="1.0"?>
        <rss version="2.0"
             xmlns:transcripts="https://podcastindex.org/namespace/1.0">
          <channel>
            <title>Systems from First Principles</title>
            <item>
              <guid>episode-guid</guid>
              <transcripts:transcript
                  url="transcripts/episode.vtt"
                  type="text/vtt"
                  language="en"
                  rel="captions" />
              <transcripts:transcript
                  url="https://cdn.example/episode.json"
                  type="application/json" />
              <enclosure url="audio/episode.mp3" type="audio/mpeg" />
            </item>
          </channel>
        </rss>
    """

    episode = parse_podcast_feed(
        feed,
        feed_url="https://feeds.example/show/feed.xml",
    )

    assert episode.transcripts == (
        PodcastTranscript(
            url="https://feeds.example/show/transcripts/episode.vtt",
            media_type="text/vtt",
            language="en",
            rel="captions",
        ),
        PodcastTranscript(
            url="https://cdn.example/episode.json",
            media_type="application/json",
            language=None,
            rel=None,
        ),
    )
    assert episode.audio_url == "https://feeds.example/show/audio/episode.mp3"


@pytest.mark.parametrize(
    ("media_type", "content", "expected"),
    [
        pytest.param(
            "text/vtt",
            b"""WEBVTT

00:00:01.000 --> 00:00:04.000
Creator supplied claim.
""",
            [("Creator supplied claim.", 1_000, 4_000)],
            id="webvtt",
        ),
        pytest.param(
            "application/x-subrip",
            b"""1
00:00:01,000 --> 00:00:04,000
Creator supplied claim.
""",
            [("Creator supplied claim.", 1_000, 4_000)],
            id="srt",
        ),
        pytest.param(
            "application/json",
            b"""{"version":"1.0.0","segments":[
                {"speaker":"Ada","startTime":1,"endTime":2,"body":"First"},
                {"speaker":"Ada","startTime":2,"endTime":4,"body":"claim."}
            ]}""",
            [("First\nclaim.", 1_000, 4_000)],
            id="json",
        ),
        pytest.param(
            "text/html",
            b"""<cite>Ada</cite><time>0:01</time><p>First claim.</p>
                <time>0:04</time><p>Supporting evidence.</p>""",
            [
                ("First claim.\nSupporting evidence.", 1_000, 8_000),
            ],
            id="html",
        ),
        pytest.param(
            "text/plain",
            b"Creator supplied claim.\n\nSupporting evidence.",
            [
                (
                    "Creator supplied claim.\nSupporting evidence.",
                    0,
                    8_000,
                ),
            ],
            id="plain-text",
        ),
    ],
)
def test_supported_creator_formats_feed_the_timestamped_transcript_pipeline(
    media_type: str,
    content: bytes,
    expected: list[tuple[str, int, int]],
) -> None:
    normalized = normalize_podcast_transcript(
        content,
        media_type=media_type,
        duration_seconds=8,
    )

    chunks = chunk_transcript(normalized)

    assert [(chunk.text, chunk.start_ms, chunk.end_ms) for chunk in chunks] == expected


def test_batch_prefers_and_records_a_creator_transcript_over_episode_audio(
    tmp_path: Path,
) -> None:
    transcript = b"""WEBVTT

00:00:01.000 --> 00:00:04.000
The creator's durable claim.
"""
    routes = {
        "/episode.vtt": (transcript, "text/vtt"),
        "/episode.mp3": (b"audio must not be fetched", "audio/mpeg"),
    }
    with _serve_resources(routes) as (base_url, handler):
        feed = f"""<?xml version="1.0"?>
            <rss version="2.0"
                 xmlns:podcast="https://podcastindex.org/namespace/1.0"
                 xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
              <channel>
                <title>A Test Podcast</title>
                <item>
                  <guid>creator-transcript</guid>
                  <itunes:duration>8</itunes:duration>
                  <podcast:transcript url="{base_url}/episode.vtt" type="text/vtt" />
                  <enclosure url="{base_url}/episode.mp3" type="audio/mpeg" />
                </item>
              </channel>
            </rss>
        """.encode()
        blob_store = _store_episode(
            tmp_path,
            feed=feed,
            feed_url=f"{base_url}/feed.xml",
        )

        result = run_podcast_transcript_batch(
            tmp_path / "modgud.sqlite3",
            blob_store,
            settings=_settings(tmp_path),
        )

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT state, extracted_text_hash FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert result == PodcastTranscriptBatchResult(
        attempted=1,
        feed_supplied=1,
        transcribed=0,
        failed=0,
    )
    assert item is not None
    assert item[0] == "extracted"
    assert blob_store.get(item[1]) == transcript
    assert handler.request_counts == {"/episode.vtt": 1}
    assert [event[0] for event in events] == [
        "captured",
        "podcast_transcript",
        "extracted",
    ]
    assert json.loads(events[-2][1]) == {
        "media_type": "text/vtt",
        "source": "feed",
        "url": f"{base_url}/episode.vtt",
    }


def test_batch_transcribes_and_records_audio_when_the_feed_has_no_transcript(
    tmp_path: Path,
) -> None:
    audio = b"pretend podcast audio"
    transcript = b"""WEBVTT

00:00:02.000 --> 00:00:05.000
Whisper recovered the useful claim.
"""
    with _serve_resources(
        {"/episode.mp3": (audio, "audio/mpeg")},
        transcription=transcript,
    ) as (base_url, handler):
        feed = f"""<?xml version="1.0"?>
            <rss version="2.0">
              <channel>
                <title>A Test Podcast</title>
                <item>
                  <guid>audio-fallback</guid>
                  <enclosure url="{base_url}/episode.mp3" type="audio/mpeg" />
                </item>
              </channel>
            </rss>
        """.encode()
        blob_store = _store_episode(
            tmp_path,
            feed=feed,
            feed_url=f"{base_url}/feed.xml",
        )

        result = run_podcast_transcript_batch(
            tmp_path / "modgud.sqlite3",
            blob_store,
            settings=_settings(tmp_path, f"{base_url}/v1"),
        )

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            "SELECT state, extracted_text_hash FROM items"
        ).fetchone()
        events = connection.execute(
            "SELECT type, payload FROM events ORDER BY id"
        ).fetchall()

    assert result == PodcastTranscriptBatchResult(
        attempted=1,
        feed_supplied=0,
        transcribed=1,
        failed=0,
    )
    assert item is not None
    assert item[0] == "extracted"
    assert blob_store.get(item[1]) == transcript
    assert handler.request_counts == {"/episode.mp3": 1}
    assert handler.post_path == "/v1/audio/transcriptions"
    assert audio in handler.post_body
    assert b"whisper-1" in handler.post_body
    assert [event[0] for event in events] == [
        "captured",
        "podcast_transcript",
        "extracted",
    ]
    assert json.loads(events[-2][1]) == {
        "source": "audio",
        "url": f"{base_url}/episode.mp3",
    }


def test_unusable_creator_transcript_does_not_block_audio_fallback(
    tmp_path: Path,
) -> None:
    transcript = b"""WEBVTT

00:00:02.000 --> 00:00:05.000
Audio fallback remained available.
"""
    with _serve_resources(
        {
            "/broken.vtt": (b"\xff", "text/vtt"),
            "/episode.mp3": (b"fallback audio", "audio/mpeg"),
        },
        transcription=transcript,
    ) as (base_url, handler):
        feed = f"""<?xml version="1.0"?>
            <rss version="2.0"
                 xmlns:podcast="https://podcastindex.org/namespace/1.0">
              <channel>
                <title>A Test Podcast</title>
                <item>
                  <guid>broken-creator-transcript</guid>
                  <podcast:transcript url="{base_url}/broken.vtt" type="text/vtt" />
                  <enclosure url="{base_url}/episode.mp3" type="audio/mpeg" />
                </item>
              </channel>
            </rss>
        """.encode()
        blob_store = _store_episode(
            tmp_path,
            feed=feed,
            feed_url=f"{base_url}/feed.xml",
        )

        result = run_podcast_transcript_batch(
            tmp_path / "modgud.sqlite3",
            blob_store,
            settings=_settings(tmp_path, f"{base_url}/v1"),
        )

    assert result == PodcastTranscriptBatchResult(
        attempted=1,
        feed_supplied=0,
        transcribed=1,
        failed=0,
    )
    assert handler.request_counts == {"/broken.vtt": 1, "/episode.mp3": 1}
