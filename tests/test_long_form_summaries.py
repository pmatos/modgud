"""Behavioral tests for on-demand tier-2 long-form summary generation."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from modgud.blobs import BlobStore
from modgud.config import Settings, get_settings
from modgud.database import connect
from modgud.long_form_summaries import (
    Tier2Summary,
    generate_long_form_summary,
    get_long_form_summary,
    request_long_form_summary,
)


class _CompletionHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []
    completion_outputs: ClassVar[list[str | None]] = []

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(content_length))
        type(self).requests.append(request)
        content = type(self).completion_outputs.pop(0)
        choices = []
        if content is not None:
            choices.append(
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            )
        response = json.dumps(
            {
                "id": "long-form-completion",
                "object": "chat.completion",
                "created": 0,
                "model": request["model"],
                "choices": choices,
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
    *responses: str | None,
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


def settings_for_tier_2_endpoint(tmp_path: Path, endpoint: str) -> Settings:
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "tier-2-config.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8").replace(
            """[models.tier_2_summary]
base_url = "http://127.0.0.1:11434/v1"
model = "gemma4:26b-a4b\"""",
            f"""[models.tier_2_summary]
base_url = "{endpoint}"
model = "gemma4:26b-a4b\"""",
        ),
        encoding="utf-8",
    )
    return get_settings(config_path)


def test_a_short_web_item_gets_one_long_form_section(tmp_path: Path) -> None:
    extracted_text = (
        "The report finds that smaller deployments reduce recovery time. "
        "It argues that reversible migrations limit operational risk."
    )
    long_form_text = (
        "This report makes a sustained case for shrinking deployment size. "
        "It walks through how smaller changes reduce blast radius and how "
        "reversible migrations keep operational risk bounded over time."
    )
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(extracted_text.encode())
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
            """,
            ("https://example.com/report", "a" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(long_form_text) as (endpoint, handler):
            result = generate_long_form_summary(
                connection,
                blob_store,
                item_id,
                settings=settings_for_tier_2_endpoint(tmp_path, endpoint),
            )

        stored = get_long_form_summary(connection, item_id)

    assert result == long_form_text
    assert stored == Tier2Summary(
        status="completed", summary_text=long_form_text, error=None
    )
    assert len(handler.requests) == 1
    assert extracted_text in json.dumps(handler.requests[0]["messages"])


def test_a_long_transcript_concatenates_a_section_per_chunk(tmp_path: Path) -> None:
    first_text = "FIRST-SECTION " + "alpha evidence " * 260
    second_text = "SECOND-SECTION " + "omega evidence " * 260
    transcript = f"""WEBVTT

00:00:00.000 --> 00:05:00.000
{first_text}

00:05:00.000 --> 00:10:00.000
{second_text}
""".encode()
    first_section = "A long-form account of the opening evidence about alpha."
    second_section = "A long-form account of the closing evidence about omega."
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source, chapters
            ) VALUES (?, ?, ?, 'youtube', 'summarized', 'Systems Workshop', '[]')
            """,
            (
                "https://www.youtube.com/watch?v=long-deployments",
                "1" * 64,
                transcript_hash,
            ),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(first_section, second_section) as (endpoint, handler):
            result = generate_long_form_summary(
                connection,
                blob_store,
                item_id,
                settings=settings_for_tier_2_endpoint(tmp_path, endpoint),
            )

    assert result is not None
    assert first_section in result
    assert second_section in result
    assert result.index(first_section) < result.index(second_section)
    assert len(handler.requests) == 2
    request_messages = [json.dumps(request["messages"]) for request in handler.requests]
    assert "FIRST-SECTION" in request_messages[0]
    assert "SECOND-SECTION" not in request_messages[0]
    assert "FIRST-SECTION" not in request_messages[1]
    assert "SECOND-SECTION" in request_messages[1]


def test_a_podcast_transcript_gets_a_long_form_section(tmp_path: Path) -> None:
    transcript = b"""WEBVTT

00:00:01.000 --> 00:00:03.500
Smaller deployments reduce recovery time.
"""
    long_form_text = (
        "A long-form account of the episode's argument for smaller deployments."
    )
    blob_store = BlobStore(tmp_path / "blobs")
    transcript_hash = blob_store.put(transcript)
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'podcast', 'summarized', 'Practical Podcast')
            """,
            (
                "https://example.com/podcasts/deployments",
                "8" * 64,
                transcript_hash,
            ),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(long_form_text) as (endpoint, handler):
            result = generate_long_form_summary(
                connection,
                blob_store,
                item_id,
                settings=settings_for_tier_2_endpoint(tmp_path, endpoint),
            )

    assert result == long_form_text
    assert len(handler.requests) == 1
    assert "Smaller deployments reduce recovery time." in json.dumps(
        handler.requests[0]["messages"]
    )


def test_repeated_empty_output_is_recorded_as_a_failure(tmp_path: Path) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about queues.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
            """,
            ("https://example.com/queues", "c" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(None, "   ") as (endpoint, handler):
            result = generate_long_form_summary(
                connection,
                blob_store,
                item_id,
                settings=settings_for_tier_2_endpoint(tmp_path, endpoint),
            )

        stored = get_long_form_summary(connection, item_id)

    assert result is None
    assert len(handler.requests) == 2
    assert stored is not None
    assert stored.status == "failed"
    assert stored.summary_text is None
    assert stored.error is not None


def test_request_long_form_summary_marks_a_new_item_pending(tmp_path: Path) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about pipelines.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
            """,
            ("https://example.com/pipelines", "d" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        started = request_long_form_summary(connection, item_id)
        stored = get_long_form_summary(connection, item_id)

    assert started is True
    assert stored == Tier2Summary(status="pending", summary_text=None, error=None)


def test_request_long_form_summary_is_a_no_op_once_pending_or_completed(
    tmp_path: Path,
) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about caches.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
            """,
            ("https://example.com/caches", "e" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        first = request_long_form_summary(connection, item_id)
        second = request_long_form_summary(connection, item_id)

    assert first is True
    assert second is False


def test_request_long_form_summary_retries_after_a_failure(tmp_path: Path) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about retries.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
            """,
            ("https://example.com/retries", "9" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(None, "   ") as (endpoint, _):
            generate_long_form_summary(
                connection,
                blob_store,
                item_id,
                settings=settings_for_tier_2_endpoint(tmp_path, endpoint),
            )
        failed = get_long_form_summary(connection, item_id)

        retried = request_long_form_summary(connection, item_id)
        pending_again = get_long_form_summary(connection, item_id)

    assert failed is not None
    assert failed.status == "failed"
    assert retried is True
    assert pending_again == Tier2Summary(
        status="pending", summary_text=None, error=None
    )


def test_a_second_request_is_free_once_completed(tmp_path: Path) -> None:
    long_form_text = "A settled long-form account of the article's argument."
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about durability.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'summarized', 'example.com')
            """,
            ("https://example.com/durability", "f" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(long_form_text) as (endpoint, handler):
            settings = settings_for_tier_2_endpoint(tmp_path, endpoint)
            request_long_form_summary(connection, item_id)
            generate_long_form_summary(
                connection, blob_store, item_id, settings=settings
            )

        again = request_long_form_summary(connection, item_id)
        stored = get_long_form_summary(connection, item_id)

    assert again is False
    assert len(handler.requests) == 1
    assert stored == Tier2Summary(
        status="completed", summary_text=long_form_text, error=None
    )
