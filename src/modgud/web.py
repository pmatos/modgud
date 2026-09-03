"""Server-rendered web application for modgud."""

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from modgud.config import Settings
from modgud.database import connect

_PACKAGE_DIRECTORY = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=_PACKAGE_DIRECTORY / "templates")


def create_app(data_dir: Path) -> FastAPI:
    """Create an application backed by the store in ``data_dir``."""
    app = FastAPI(title="modgud")
    app.mount(
        "/static",
        StaticFiles(directory=_PACKAGE_DIRECTORY / "static"),
        name="static",
    )
    database = data_dir / "modgud.sqlite3"

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        with connect(database) as connection:
            item_count = int(
                connection.execute("SELECT count(*) FROM items").fetchone()[0]
            )
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"item_count": item_count},
        )

    return app


def serve(settings: Settings, data_dir: Path) -> None:
    """Serve modgud on the interface selected by the operator."""
    data_dir.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        create_app(data_dir),
        host=settings.web_bind.host,
        port=settings.web_bind.port,
    )
