"""Behavioral tests for timestamped span-map generation."""

import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from modgud.blobs import BlobStore
from modgud.cli import main
from modgud.config import Settings, get_settings
from modgud.database import connect
from modgud.span_maps import Span, SpanMap, generate_span_map, get_span_map
from modgud.transcripts import chunk_transcript
from modgud.youtube import Chapter


class _CompletionHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []
    completion_outputs: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(content_length))
        type(self).requests.append(request)
        content = type(self).completion_outputs.pop(0)
        response = json.dumps(
            {
                "id": "span-map-completion",
                "object": "chat.completion",
                "created": 0,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve_completions(
    *responses: str,
) -> Iterator[tuple[str, type[_CompletionHandler]]]:
    class Handler(_CompletionHandler):
        pass

    Handler.requests = []
    Handler.completion_outputs = list(responses)
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


def settings_for_span_endpoint(tmp_path: Path, endpoint: str) -> Settings:
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "span-map-config.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8").replace(
            """[models.span_map]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b""",
            f"""[models.span_map]
base_url = "{endpoint}"
model = "span-map-test-model""",
        ),
        encoding="utf-8",
    )
    return get_settings(config_path)


def test_selected_chunk_becomes_a_stored_span_with_structural_timestamps(
    tmp_path: Path,
) -> None:
    transcript = b"""WEBVTT

00:00:00.000 --> 00:00:02.500
Opening context that is not selected.

00:00:10.250 --> 00:00:13.750
A practical technique worth trying.
"""
    chapters: list[Chapter] = [
        {"start_time": 0.0, "end_time": 10.0, "title": "Opening"},
        {"start_time": 10.0, "end_time": 14.0, "title": "Technique"},
    ]
    selected_id = chunk_transcript(transcript, chapters=chapters)[1].id
    model_output = json.dumps(
        {
            "chunks": [
                {
                    "id": selected_id,
                    "description": "A concrete technique with an immediately useful example.",
                }
            ]
        }
    )
    expected = SpanMap(
        spans=(
            Span(
                start_ms=10_250,
                end_ms=13_750,
                description="A concrete technique with an immediately useful example.",
            ),
        )
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)

    with serve_completions(model_output) as (endpoint, handler):
        with connect(tmp_path / "modgud.sqlite3") as connection:
            item_id = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source, chapters
                ) VALUES (?, ?, ?, 'youtube', 'extracted', ?, ?)
                """,
                (
                    "https://www.youtube.com/watch?v=span-map",
                    "a" * 64,
                    transcript_hash,
                    "Practical Channel",
                    json.dumps(chapters),
                ),
            ).lastrowid
            assert item_id is not None
            generated = generate_span_map(
                connection,
                blob_store,
                item_id,
                settings=settings_for_span_endpoint(tmp_path, endpoint),
            )

        with connect(tmp_path / "modgud.sqlite3") as connection:
            stored = get_span_map(connection, item_id)

    assert generated == expected
    assert stored == expected
    assert handler.requests[0]["model"] == "span-map-test-model"
    messages = json.dumps(handler.requests[0]["messages"])
    assert selected_id in messages
    assert "A practical technique worth trying." in messages
    assert "00:00:" not in messages
    assert "start_ms" not in messages


def test_adjacent_selected_chunks_merge_in_transcript_order(tmp_path: Path) -> None:
    transcript = b"""WEBVTT

00:00:00.000 --> 00:00:02.000
First worthwhile part.

00:00:10.000 --> 00:00:12.000
Second worthwhile part.

00:00:20.000 --> 00:00:22.000
Unselected connective material.

00:00:30.000 --> 00:00:32.000
A later independent highlight.
"""
    chapters: list[Chapter] = [
        {"start_time": float(start), "end_time": float(start + 10), "title": title}
        for start, title in (
            (0, "First"),
            (10, "Second"),
            (20, "Context"),
            (30, "Later"),
        )
    ]
    chunks = chunk_transcript(transcript, chapters=chapters)
    model_output = json.dumps(
        {
            "chunks": [
                {
                    "id": chunks[3].id,
                    "description": "A later highlight that stands on its own.",
                },
                {
                    "id": chunks[1].id,
                    "description": "The example shows the idea in practice.",
                },
                {
                    "id": chunks[0].id,
                    "description": "The core idea is introduced clearly.",
                },
            ]
        }
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)

    with (
        serve_completions(model_output) as (endpoint, _),
        connect(tmp_path / "modgud.sqlite3") as connection,
    ):
        item_id = connection.execute(
            """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source, chapters
                ) VALUES (?, ?, ?, 'youtube', 'extracted', ?, ?)
                """,
            (
                "https://www.youtube.com/watch?v=adjacent-spans",
                "b" * 64,
                transcript_hash,
                "Practical Channel",
                json.dumps(chapters),
            ),
        ).lastrowid
        assert item_id is not None
        generated = generate_span_map(
            connection,
            blob_store,
            item_id,
            settings=settings_for_span_endpoint(tmp_path, endpoint),
        )

    assert generated == SpanMap(
        spans=(
            Span(
                start_ms=0,
                end_ms=12_000,
                description=(
                    "The core idea is introduced clearly. "
                    "The example shows the idea in practice."
                ),
            ),
            Span(
                start_ms=30_000,
                end_ms=32_000,
                description="A later highlight that stands on its own.",
            ),
        )
    )


def test_unknown_chunk_id_rejects_the_complete_model_response(tmp_path: Path) -> None:
    transcript = b"""WEBVTT

00:00:01.000 --> 00:00:03.000
A real chunk with useful content.
"""
    real_id = chunk_transcript(transcript)[0].id
    invalid_output = json.dumps(
        {
            "chunks": [
                {"id": real_id, "description": "This selection is real."},
                {
                    "id": "chunk-does-not-exist",
                    "description": "This selection was hallucinated.",
                },
            ]
        }
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)

    with (
        serve_completions(invalid_output, invalid_output) as (endpoint, handler),
        connect(tmp_path / "modgud.sqlite3") as connection,
    ):
        item_id = connection.execute(
            """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source
                ) VALUES (?, ?, ?, 'youtube', 'extracted', ?)
                """,
            (
                "https://www.youtube.com/watch?v=unknown-chunk",
                "c" * 64,
                transcript_hash,
                "Practical Channel",
            ),
        ).lastrowid
        assert item_id is not None
        generated = generate_span_map(
            connection,
            blob_store,
            item_id,
            settings=settings_for_span_endpoint(tmp_path, endpoint),
        )
        stored = get_span_map(connection, item_id)

    assert generated is None
    assert stored is None
    assert len(handler.requests) == 2


def test_multiline_chunk_description_is_rejected_before_storage(tmp_path: Path) -> None:
    transcript = b"""WEBVTT

00:00:04.000 --> 00:00:06.000
A concise explanation of the key result.
"""
    chunk_id = chunk_transcript(transcript)[0].id
    multiline_output = json.dumps(
        {
            "chunks": [
                {
                    "id": chunk_id,
                    "description": "The key result is explained.\nWith a useful caveat.",
                }
            ]
        }
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)

    with (
        serve_completions(multiline_output, multiline_output) as (endpoint, handler),
        connect(tmp_path / "modgud.sqlite3") as connection,
    ):
        item_id = connection.execute(
            """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source
                ) VALUES (?, ?, ?, 'youtube', 'extracted', ?)
                """,
            (
                "https://www.youtube.com/watch?v=multiline-description",
                "d" * 64,
                transcript_hash,
                "Practical Channel",
            ),
        ).lastrowid
        assert item_id is not None
        generated = generate_span_map(
            connection,
            blob_store,
            item_id,
            settings=settings_for_span_endpoint(tmp_path, endpoint),
        )
        stored = get_span_map(connection, item_id)

    assert generated is None
    assert stored is None
    assert len(handler.requests) == 2


def test_duplicate_chunk_id_is_rejected(tmp_path: Path) -> None:
    transcript = b"""WEBVTT

00:00:07.000 --> 00:00:09.000
One chunk must not become two selections.
"""
    chunk_id = chunk_transcript(transcript)[0].id
    duplicate_output = json.dumps(
        {
            "chunks": [
                {"id": chunk_id, "description": "The same selected value."},
                {"id": chunk_id, "description": "The same selected value."},
            ]
        }
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)

    with (
        serve_completions(duplicate_output, duplicate_output) as (endpoint, handler),
        connect(tmp_path / "modgud.sqlite3") as connection,
    ):
        item_id = connection.execute(
            """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source
                ) VALUES (?, ?, ?, 'youtube', 'extracted', ?)
                """,
            (
                "https://www.youtube.com/watch?v=duplicate-chunk",
                "e" * 64,
                transcript_hash,
                "Practical Channel",
            ),
        ).lastrowid
        assert item_id is not None
        generated = generate_span_map(
            connection,
            blob_store,
            item_id,
            settings=settings_for_span_endpoint(tmp_path, endpoint),
        )
        stored = get_span_map(connection, item_id)

    assert generated is None
    assert stored is None
    assert len(handler.requests) == 2


def test_span_map_command_generates_the_item_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transcript = b"""WEBVTT

00:00:15.000 --> 00:00:18.000
A command-line generated highlight.
"""
    chunk_id = chunk_transcript(transcript)[0].id
    model_output = json.dumps(
        {
            "chunks": [
                {
                    "id": chunk_id,
                    "description": "A useful highlight generated on demand.",
                }
            ]
        }
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)

    with serve_completions(model_output) as (endpoint, _):
        settings_for_span_endpoint(tmp_path, endpoint)
        with connect(tmp_path / "modgud.sqlite3") as connection:
            item_id = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source
                ) VALUES (?, ?, ?, 'youtube', 'extracted', ?)
                """,
                (
                    "https://www.youtube.com/watch?v=span-map-command",
                    "f" * 64,
                    transcript_hash,
                    "Practical Channel",
                ),
            ).lastrowid
            assert item_id is not None
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "modgud",
                "--config",
                str(tmp_path / "span-map-config.toml"),
                "--data-dir",
                str(tmp_path),
                "span-map",
                str(item_id),
            ],
        )

        main()

    with connect(tmp_path / "modgud.sqlite3") as connection:
        stored = get_span_map(connection, item_id)

    assert capsys.readouterr().out == f"Generated span map for item {item_id}\n"
    assert stored == SpanMap(
        spans=(
            Span(
                start_ms=15_000,
                end_ms=18_000,
                description="A useful highlight generated on demand.",
            ),
        )
    )
