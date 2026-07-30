"""FastAPI application factory.

Creates the app with DB initialization, auth, API v1 router, and web frontend.
Supports dependency injection for testing (custom conn, settings, time).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Scope

from .api.v1 import create_v1_router
from .auth import make_auth_dependency
from .config import Settings
from .db import init_schema
from .web.routes import create_web_router

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
STATIC_DIR = Path(__file__).parent / "web" / "static"


def _static_asset_version() -> str:
    """Return a short content hash over packaged public static assets."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:16]


ASSET_VERSION = _static_asset_version()


class CacheControlledStaticFiles(StaticFiles):
    """Serve packaged assets with a bounded public cache policy.

    Browsers and CDNs may cache assets for up to one hour. Templates append a
    content hash query parameter when linking static files, so a bounded cache
    stays cheap while still refreshing after asset changes.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response


def create_app(
    settings: Settings | None = None,
    *,
    get_conn: Any = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Application settings. Defaults to env-based Settings.
        get_conn: Callable returning a sqlite3.Connection. If None, a
            connection is opened lazily from settings.db_path and schema is
            initialized.
    """
    settings = settings or Settings.from_env()

    _owns_conn = get_conn is None
    if _owns_conn:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        init_schema(_conn)

        def _get_conn() -> sqlite3.Connection:
            return _conn
    else:
        _get_conn = get_conn  # caller manages lifecycle (tests)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = True  # explicit; Jinja2Templates defaults vary

    app = FastAPI(
        title="Hermes Tracker",
        version="0.1.0",
        description="Deliberately small agent-only TODO tracker.",
        root_path=settings.root_path,
    )
    app.state.settings = settings
    app.state.asset_version = ASSET_VERSION
    app.state._get_conn = _get_conn
    app.state._owns_conn = _owns_conn

    app.mount(
        "/static",
        CacheControlledStaticFiles(directory=STATIC_DIR),
        name="static",
    )

    auth = make_auth_dependency(settings, _get_conn)

    api_router = create_v1_router(_get_conn, auth)
    app.include_router(api_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """Return minimal liveness information for service checks."""
        return {"status": "ok"}

    web_router = create_web_router(_get_conn, templates, settings.cgit_base)
    app.include_router(web_router)

    return app


# Module-level app for `uvicorn hermes_tracker.app:app`.
# Eagerly created so uvicorn finds a real ASGI callable, not None.
app = create_app()
