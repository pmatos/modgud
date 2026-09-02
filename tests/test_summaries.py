"""Behavioral tests for tier-1 summary generation."""

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
from modgud.summaries import Tier1Summary, summarize_item


class _CompletionHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []
    completion_outputs: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        response = b"""
            <html><body><article>
              <h1>Designing Durable Queues</h1>
              <p>Durable queues persist pending work across process restarts.</p>
              <p>Consumers acknowledge work only after it has completed.</p>
              <p>Idempotent handlers make redelivery safe.</p>
            </article></body></html>
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

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
                "id": "summary-completion",
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


def settings_for_summary_endpoint(tmp_path: Path, endpoint: str) -> Settings:
    example = Path(__file__).parents[1] / "config.example.toml"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        example.read_text(encoding="utf-8").replace(
            'base_url = "http://127.0.0.1:11434/v1"',
            f'base_url = "{endpoint}"',
            1,
        ),
        encoding="utf-8",
    )
    return get_settings(config_path)


def test_extracted_web_item_gets_structured_summary_from_configured_route(
    tmp_path: Path,
) -> None:
    extracted_text = (
        "The report finds that smaller deployments reduce recovery time. "
        "It argues that reversible migrations limit operational risk."
    )
    expected = Tier1Summary(
        one_liner="A report on reducing deployment risk through smaller changes.",
        claims=(
            "Smaller deployments reduce recovery time.",
            "Reversible migrations limit operational risk.",
            "Deployment size is a practical reliability control.",
        ),
    )
    model_output = json.dumps(
        {"one_liner": expected.one_liner, "claims": expected.claims}
    )
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(extracted_text.encode())
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'extracted', 'example.com')
            """,
            ("https://example.com/report", "a" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(model_output) as (endpoint, handler):
            result = summarize_item(
                connection,
                blob_store,
                item_id,
                settings=settings_for_summary_endpoint(tmp_path, endpoint),
            )

        stored = connection.execute(
            "SELECT one_liner, claims FROM tier_1_summaries WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        state = connection.execute(
            "SELECT state FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]

    assert result == expected
    assert stored == (expected.one_liner, json.dumps(list(expected.claims)))
    assert state == "summarized"
    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert request["model"] == "gemma4:26b-a4b"
    assert extracted_text in json.dumps(request["messages"])


def test_malformed_model_output_is_retried(tmp_path: Path) -> None:
    expected = Tier1Summary(
        one_liner="An article about keeping database migrations reversible.",
        claims=(
            "Reversible migrations reduce deployment risk.",
            "Expand-and-contract changes keep old code compatible.",
            "Rollback plans should be exercised before deployment.",
        ),
    )
    valid_output = json.dumps(
        {"one_liner": expected.one_liner, "claims": expected.claims}
    )
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about migrations.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'extracted', 'example.com')
            """,
            ("https://example.com/migrations", "b" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(None, valid_output) as (endpoint, handler):
            result = summarize_item(
                connection,
                blob_store,
                item_id,
                settings=settings_for_summary_endpoint(tmp_path, endpoint),
            )

    assert result == expected
    assert len(handler.requests) == 2


def test_repeated_malformed_output_is_recorded_as_a_failure(tmp_path: Path) -> None:
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about queues.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'extracted', 'example.com')
            """,
            ("https://example.com/queues", "c" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions("not json", '{"claims": []}') as (
            endpoint,
            handler,
        ):
            result = summarize_item(
                connection,
                blob_store,
                item_id,
                settings=settings_for_summary_endpoint(tmp_path, endpoint),
            )

        stored_count = connection.execute(
            "SELECT count(*) FROM tier_1_summaries WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
        state = connection.execute(
            "SELECT state FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]
        failure = connection.execute(
            "SELECT type, payload FROM events WHERE item_id = ?", (item_id,)
        ).fetchone()

    assert result is None
    assert len(handler.requests) == 2
    assert stored_count == 0
    assert state == "failed"
    assert failure[0] == "failed"
    assert json.loads(failure[1]) == {
        "attempts": 2,
        "error": "model returned malformed summary output",
        "stage": "summary",
    }


def test_regenerating_an_item_replaces_its_artifact_and_logs_an_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = {
        "one_liner": "The first summary of an article about durable queues.",
        "claims": [
            "Durable queues retain work across restarts.",
            "Acknowledgements distinguish pending work from completed work.",
            "Retries require idempotent consumers.",
        ],
    }
    replacement = {
        "one_liner": "An article explaining the guarantees behind durable queues.",
        "claims": [
            "Persistence prevents queued work from disappearing on restart.",
            "Acknowledging only completed work enables safe redelivery.",
            "Idempotency makes repeated delivery harmless.",
            "Backpressure keeps producers from overwhelming consumers.",
        ],
    }
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted article about durable queues.")

    with serve_completions(json.dumps(first), json.dumps(replacement)) as (
        endpoint,
        _,
    ):
        example = Path(__file__).parents[1] / "config.example.toml"
        config_path = tmp_path / "regeneration-config.toml"
        config_path.write_text(
            example.read_text(encoding="utf-8").replace(
                'base_url = "http://127.0.0.1:11434/v1"',
                f'base_url = "{endpoint}"',
                1,
            ),
            encoding="utf-8",
        )
        settings = get_settings(config_path)
        with connect(tmp_path / "modgud.sqlite3") as connection:
            cursor = connection.execute(
                """
                INSERT INTO items (
                    canonical_url, content_hash, extracted_text_hash,
                    format, state, source
                ) VALUES (?, ?, ?, 'web', 'extracted', 'example.com')
                """,
                ("https://example.com/durable-queues", "d" * 64, extracted_text_hash),
            )
            item_id = cursor.lastrowid
            assert item_id is not None
            summarize_item(
                connection,
                blob_store,
                item_id,
                settings=settings,
            )

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "modgud",
                "--config",
                str(config_path),
                "--data-dir",
                str(tmp_path),
                "summarize",
                str(item_id),
            ],
        )
        main()

    with connect(tmp_path / "modgud.sqlite3") as connection:
        artifacts = connection.execute(
            "SELECT one_liner, claims FROM tier_1_summaries WHERE item_id = ?",
            (item_id,),
        ).fetchall()
        events = connection.execute(
            """
            SELECT payload FROM events
            WHERE item_id = ? AND type = 'summarized'
            ORDER BY id
            """,
            (item_id,),
        ).fetchall()

    assert capsys.readouterr().out == f"Summarized item {item_id}\n"
    assert artifacts == [(replacement["one_liner"], json.dumps(replacement["claims"]))]
    assert [json.loads(event[0])["one_liner"] for event in events] == [
        first["one_liner"],
        replacement["one_liner"],
    ]


def test_adding_an_extracted_web_item_generates_its_summary_on_arrival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model_output = {
        "one_liner": "An article explaining reliable durable-queue consumers.",
        "claims": [
            "Durable queues persist pending work across restarts.",
            "Consumers should acknowledge work only after completion.",
            "Idempotent handlers make redelivery safe.",
        ],
    }
    with serve_completions(json.dumps(model_output)) as (endpoint, handler):
        example = Path(__file__).parents[1] / "config.example.toml"
        config_path = tmp_path / "add-config.toml"
        config_path.write_text(
            example.read_text(encoding="utf-8").replace(
                'base_url = "http://127.0.0.1:11434/v1"',
                f'base_url = "{endpoint}"',
                1,
            ),
            encoding="utf-8",
        )
        article_url = endpoint.removesuffix("/v1") + "/article"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "modgud",
                "--config",
                str(config_path),
                "--data-dir",
                str(tmp_path),
                "add",
                article_url,
            ],
        )

        main()

    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            SELECT items.state, tier_1_summaries.one_liner,
                   tier_1_summaries.claims
            FROM items
            JOIN tier_1_summaries ON tier_1_summaries.item_id = items.id
            """
        ).fetchone()
        event_types = [
            event[0]
            for event in connection.execute("SELECT type FROM events ORDER BY id")
        ]

    assert capsys.readouterr().out == f"Added item 1: {article_url}\n"
    assert item == (
        "summarized",
        model_output["one_liner"],
        json.dumps(model_output["claims"]),
    )
    assert event_types == ["captured", "extracted", "summarized"]
    assert len(handler.requests) == 1


def test_failed_regeneration_keeps_the_last_valid_artifact(tmp_path: Path) -> None:
    original = {
        "one_liner": "An article about retaining useful work through restarts.",
        "claims": [
            "Pending work must be persisted before acknowledging producers.",
            "Consumers acknowledge work only after completing it.",
            "Redelivery recovers work interrupted by a restart.",
        ],
    }
    blob_store = BlobStore(tmp_path / "blobs")
    extracted_text_hash = blob_store.put(b"An extracted queue article.")
    with connect(tmp_path / "modgud.sqlite3") as connection:
        cursor = connection.execute(
            """
            INSERT INTO items (
                canonical_url, content_hash, extracted_text_hash,
                format, state, source
            ) VALUES (?, ?, ?, 'web', 'extracted', 'example.com')
            """,
            ("https://example.com/restarts", "e" * 64, extracted_text_hash),
        )
        item_id = cursor.lastrowid
        assert item_id is not None

        with serve_completions(json.dumps(original), "bad", "still bad") as (
            endpoint,
            _,
        ):
            settings = settings_for_summary_endpoint(tmp_path, endpoint)
            summarize_item(
                connection,
                blob_store,
                item_id,
                settings=settings,
            )
            result = summarize_item(
                connection,
                blob_store,
                item_id,
                settings=settings,
            )

        artifact = connection.execute(
            "SELECT one_liner, claims FROM tier_1_summaries WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        state = connection.execute(
            "SELECT state FROM items WHERE id = ?", (item_id,)
        ).fetchone()[0]
        event_types = [
            event[0]
            for event in connection.execute(
                "SELECT type FROM events WHERE item_id = ? ORDER BY id", (item_id,)
            )
        ]

    assert result is None
    assert artifact == (original["one_liner"], json.dumps(original["claims"]))
    assert state == "summarized"
    assert event_types == ["summarized", "failed"]
