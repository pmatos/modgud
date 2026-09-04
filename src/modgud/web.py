"""Server-rendered web application for modgud."""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from modgud.blobs import BlobStore
from modgud.cli import capture_url
from modgud.config import Settings
from modgud.database import connect
from modgud.formats import TRANSCRIPT_FORMATS, ItemFormat
from modgud.label_tokens import (
    ExpiredLabelToken,
    InvalidLabelToken,
    validate_label_token,
)
from modgud.long_form_summaries import (
    LONG_FORM_SUMMARY_FORMATS,
    generate_long_form_summary,
    get_long_form_summary,
    request_long_form_summary,
)
from modgud.span_maps import load_transcript_chunks
from modgud.transcripts import chunk_anchors, format_timestamp

_PACKAGE_DIRECTORY = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=_PACKAGE_DIRECTORY / "templates")
_ITEM_FORMATS = tuple(item_format.value for item_format in ItemFormat)
_ITEM_STATES = (
    "captured",
    "extracted",
    "summarized",
    "unsummarizable",
    "failed",
)
_LABEL_NAMES = {
    "worth-it": "Worth it",
    "not-worth-it": "Not worth it",
}


@dataclass(frozen=True, slots=True)
class ItemListEntry:
    """Display-ready fields for one captured item."""

    item_id: int
    title: str
    canonical_url: str
    source: str
    item_format: str
    state: str
    captured_at: str
    captured_at_label: str
    time_to_value: str


@dataclass(frozen=True, slots=True)
class TranscriptChunkEntry:
    """Display-ready fields for one rendered transcript chunk."""

    anchor: str | None
    timestamp: str
    text: str


def _format_time_to_value(seconds: int | None) -> str:
    if seconds is None:
        return "Not available"
    minutes = (seconds + 59) // 60
    return f"{minutes} min"


def _format_captured_at(value: str) -> str:
    try:
        captured_at = datetime.fromisoformat(value)
    except ValueError:
        return value
    return f"{captured_at.day} {captured_at:%b %Y · %H:%M UTC}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    data_dir: Path,
    *,
    settings: Settings | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> FastAPI:
    """Create an application backed by the store in ``data_dir``."""
    app = FastAPI(title="modgud")
    app.mount(
        "/static",
        StaticFiles(directory=_PACKAGE_DIRECTORY / "static"),
        name="static",
    )
    database = data_dir / "modgud.sqlite3"
    blob_store = BlobStore(data_dir / "blobs")

    with connect(database) as connection:
        # A generation thread cannot survive a process restart, so anything
        # still "pending" at startup was interrupted and must not hang forever.
        connection.execute(
            """
            UPDATE tier_2_summaries
            SET status = 'failed',
                summary_text = NULL,
                error = 'interrupted before completion',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE status = 'pending'
            """
        )

    def _error_response(
        request: Request, template_name: str, message: str, *, status_code: int = 404
    ) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request=request,
            name=template_name,
            context={"message": message},
            status_code=status_code,
        )

    def label_error(
        request: Request, message: str, *, status_code: int = 404
    ) -> HTMLResponse:
        return _error_response(
            request, "label_error.html", message, status_code=status_code
        )

    def item_error(
        request: Request, message: str, *, status_code: int = 404
    ) -> HTMLResponse:
        return _error_response(
            request, "item_error.html", message, status_code=status_code
        )

    def invalid_label_token(
        request: Request,
        item_id: int,
        label: str,
    ) -> HTMLResponse | None:
        signing_secret = (
            settings.secrets.label_token_secret if settings is not None else None
        )
        token = request.query_params.get("token")
        if signing_secret is None or token is None:
            return label_error(
                request,
                "This label link is invalid or incomplete",
                status_code=400,
            )
        try:
            validate_label_token(
                token,
                item_id,
                label,
                signing_secret=signing_secret,
                now=clock(),
            )
        except ExpiredLabelToken:
            return label_error(
                request,
                "This label link has expired",
                status_code=410,
            )
        except InvalidLabelToken as error:
            message = (
                "This label link is not valid for this item"
                if str(error) == "wrong item"
                else "This label link is not valid for this opinion"
                if str(error) == "wrong label"
                else "This label link is invalid or incomplete"
            )
            return label_error(request, message, status_code=400)
        return None

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        capture: str | None = None,
        item: int | None = None,
        item_format: str | None = Query(default=None, alias="format"),
        state: str | None = None,
    ) -> HTMLResponse:
        active_format = item_format if item_format in _ITEM_FORMATS else None
        active_state = state if state in _ITEM_STATES else None
        with connect(database) as connection:
            item_count = int(
                connection.execute("SELECT count(*) FROM items").fetchone()[0]
            )
            captured_item = None
            if capture in {"added", "existing"} and item is not None:
                captured_item = connection.execute(
                    "SELECT id, canonical_url FROM items WHERE id = ?",
                    (item,),
                ).fetchone()
            rows = connection.execute(
                """
                SELECT items.id,
                       coalesce(items.title, items.canonical_url),
                       items.canonical_url,
                       items.source,
                       items.format,
                       items.state,
                       max(events.created_at),
                       items.time_to_value_seconds
                FROM items
                JOIN events ON events.item_id = items.id
                WHERE events.type = 'captured'
                  AND (? IS NULL OR items.format = ?)
                  AND (? IS NULL OR items.state = ?)
                GROUP BY items.id
                ORDER BY max(events.created_at) DESC, items.id DESC
                """,
                (active_format, active_format, active_state, active_state),
            ).fetchall()
        items = [
            ItemListEntry(
                item_id=int(row[0]),
                title=str(row[1]),
                canonical_url=str(row[2]),
                source=str(row[3]),
                item_format=str(row[4]),
                state=str(row[5]),
                captured_at=str(row[6]),
                captured_at_label=_format_captured_at(str(row[6])),
                time_to_value=_format_time_to_value(
                    int(row[7]) if row[7] is not None else None
                ),
            )
            for row in rows
        ]
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "capture": capture,
                "capture_error": capture == "invalid",
                "captured_item": captured_item,
                "active_format": active_format,
                "active_state": active_state,
                "item_formats": _ITEM_FORMATS,
                "item_states": _ITEM_STATES,
                "item_count": item_count,
                "items": items,
            },
        )

    @app.post("/")
    async def drop(request: Request) -> Response:
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        url = form.get("url", [""])[0].strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.hostname is None:
            response = home(
                request,
                capture="invalid",
                item=None,
                item_format=None,
                state=None,
            )
            response.status_code = 422
            return response
        result = await run_in_threadpool(capture_url, data_dir, url, settings)
        if result is None:
            raise RuntimeError("A manual capture must produce a result")
        query = urlencode(
            {
                "capture": "added" if result.created else "existing",
                "item": result.item_id,
            }
        )
        return RedirectResponse(f"/?{query}", status_code=303)

    @app.get("/items/{item_id}", response_class=HTMLResponse)
    def item_transcript(request: Request, item_id: int) -> HTMLResponse:
        with connect(database) as connection:
            item = connection.execute(
                """
                SELECT coalesce(title, canonical_url), canonical_url, source,
                       format, extracted_text_hash, chapters
                FROM items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        if item is None:
            return item_error(request, "Item not found")
        (
            title,
            canonical_url,
            source,
            item_format,
            extracted_text_hash,
            chapters_json,
        ) = item
        if item_format not in TRANSCRIPT_FORMATS or extracted_text_hash is None:
            return item_error(request, "No transcript is available for this item")
        try:
            chunks = load_transcript_chunks(
                blob_store,
                str(extracted_text_hash),
                chapters_json,
                item_id=item_id,
            )
        except (OSError, ValueError, TypeError):
            return item_error(request, "No transcript is available for this item")
        anchors = chunk_anchors(chunks)
        entries = [
            TranscriptChunkEntry(
                anchor=anchors.get(chunk.id),
                timestamp=format_timestamp(chunk.start_ms),
                text=chunk.text,
            )
            for chunk in chunks
        ]
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="transcript.html",
            context={
                "title": title,
                "canonical_url": canonical_url,
                "source": source,
                "chunks": entries,
            },
        )

    def _run_long_form_summary(item_id: int, active_settings: Settings) -> None:
        try:
            with connect(database) as connection:
                generate_long_form_summary(
                    connection, blob_store, item_id, settings=active_settings
                )
        except Exception as error:  # noqa: BLE001 - must surface, never hang pending
            error_message = str(error).strip() or type(error).__name__
            with connect(database) as connection:
                connection.execute(
                    """
                    UPDATE tier_2_summaries
                    SET status = 'failed',
                        summary_text = NULL,
                        error = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE item_id = ?
                    """,
                    (error_message, item_id),
                )

    @app.get("/items/{item_id}/summary", response_class=HTMLResponse)
    def item_long_form_summary(request: Request, item_id: int) -> HTMLResponse:
        with connect(database) as connection:
            item = connection.execute(
                """
                SELECT coalesce(title, canonical_url), canonical_url, source,
                       format, extracted_text_hash
                FROM items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if item is None:
                return item_error(request, "Item not found")
            title, canonical_url, source, item_format, extracted_text_hash = item
            summary = get_long_form_summary(connection, item_id)
        eligible = (
            item_format in LONG_FORM_SUMMARY_FORMATS and extracted_text_hash is not None
        )
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="long_form_summary.html",
            context={
                "title": title,
                "canonical_url": canonical_url,
                "source": source,
                "eligible": eligible,
                "summary": summary,
            },
        )

    @app.post("/items/{item_id}/summary")
    def request_item_long_form_summary(request: Request, item_id: int) -> Response:
        with connect(database) as connection:
            item = connection.execute(
                "SELECT format, extracted_text_hash FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None:
                return item_error(request, "Item not found")
            item_format, extracted_text_hash = item
            if (
                item_format not in LONG_FORM_SUMMARY_FORMATS
                or extracted_text_hash is None
            ):
                return item_error(
                    request,
                    "No content is available to summarize for this item",
                    status_code=400,
                )
            if settings is None:
                return item_error(
                    request,
                    "Model routing is not configured for this server",
                    status_code=500,
                )
            started = request_long_form_summary(connection, item_id)
        if started:
            threading.Thread(
                target=_run_long_form_summary,
                args=(item_id, settings),
                daemon=True,
            ).start()
        return RedirectResponse(f"/items/{item_id}/summary", status_code=303)

    @app.get("/items/{item_id}/labels/{label}", response_class=HTMLResponse)
    def confirm_label(request: Request, item_id: int, label: str) -> HTMLResponse:
        label_name = _LABEL_NAMES.get(label)
        if label_name is None:
            return label_error(request, "Label not recognized")
        token_error = invalid_label_token(request, item_id, label)
        if token_error is not None:
            return token_error
        with connect(database) as connection:
            item = connection.execute(
                """
                SELECT coalesce(title, canonical_url), canonical_url
                FROM items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        if item is None:
            return label_error(request, "Item not found")
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="confirm_label.html",
            context={
                "item": item,
                "label": label,
                "label_name": label_name,
            },
        )

    @app.post("/items/{item_id}/labels/{label}", response_class=HTMLResponse)
    def record_label(request: Request, item_id: int, label: str) -> HTMLResponse:
        label_name = _LABEL_NAMES.get(label)
        if label_name is None:
            return label_error(request, "Label not recognized")
        token_error = invalid_label_token(request, item_id, label)
        if token_error is not None:
            return token_error
        with connect(database) as connection:
            item = connection.execute(
                """
                SELECT coalesce(title, canonical_url), canonical_url
                FROM items
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            if item is None:
                return label_error(request, "Item not found")
            connection.execute(
                "INSERT INTO events (item_id, type, payload) VALUES (?, 'label', ?)",
                (item_id, json.dumps({"label": label}, separators=(",", ":"))),
            )
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="label_recorded.html",
            context={
                "item": item,
                "label_name": label_name,
            },
        )

    return app


def serve(settings: Settings, data_dir: Path) -> None:
    """Serve modgud on the interface selected by the operator."""
    data_dir.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        create_app(data_dir, settings=settings),
        host=settings.web_bind.host,
        port=settings.web_bind.port,
    )
