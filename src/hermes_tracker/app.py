"""FastAPI application factory with request-scoped SQLite ownership."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Scope

from .api.v1 import create_v1_router
from .auth import make_auth_dependency
from .config import Settings, normalize_cgit_base
from .db import connect, init_schema
from .web.routes import create_web_router

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
STATIC_DIR = Path(__file__).parent / "web" / "static"


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


ASSET_VERSION = _static_asset_version()


class CacheControlledStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response


def create_app(
    settings: Settings | None = None,
    *,
    get_conn: Callable[[], sqlite3.Connection] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    cgit_base = normalize_cgit_base(settings.cgit_base)
    connection_factory = get_conn or (lambda: connect(settings.db_path))
    setup = connection_factory()
    try:
        init_schema(setup)
    finally:
        setup.close()

    def connection() -> Iterator[sqlite3.Connection]:
        conn = connection_factory()
        try:
            yield conn
        finally:
            conn.close()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = True
    app = FastAPI(
        title="Hermes Tracker",
        version="0.1.0",
        description="Small authenticated project issue ledger.",
        root_path=settings.root_path,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.asset_version = ASSET_VERSION
    app.state.connection_factory = connection_factory
    app.mount("/static", CacheControlledStaticFiles(directory=STATIC_DIR), name="static")

    auth = make_auth_dependency(settings, connection)

    @app.get("/api/v1/openapi.json", include_in_schema=False, dependencies=[Depends(auth)])
    def private_openapi() -> JSONResponse:
        return JSONResponse(
            get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
                servers=[{"url": settings.root_path}] if settings.root_path else None,
            )
        )

    @app.get("/api/v1/docs", include_in_schema=False, dependencies=[Depends(auth)])
    def private_docs() -> Response:
        return get_swagger_ui_html(
            openapi_url=f"{settings.root_path}/api/v1/openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    app.include_router(create_v1_router(connection, auth, cgit_base))

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(create_web_router(connection, templates, cgit_base))
    return app


app = create_app()
