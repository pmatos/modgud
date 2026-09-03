"""Server-rendered web application for modgud."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from modgud.cli import capture_url
from modgud.config import Settings
from modgud.database import connect
from modgud.formats import ItemFormat

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


def create_app(data_dir: Path, *, settings: Settings | None = None) -> FastAPI:
    """Create an application backed by the store in ``data_dir``."""
    app = FastAPI(title="modgud")
    app.mount(
        "/static",
        StaticFiles(directory=_PACKAGE_DIRECTORY / "static"),
        name="static",
    )
    database = data_dir / "modgud.sqlite3"

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

    return app


def serve(settings: Settings, data_dir: Path) -> None:
    """Serve modgud on the interface selected by the operator."""
    data_dir.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        create_app(data_dir, settings=settings),
        host=settings.web_bind.host,
        port=settings.web_bind.port,
    )
