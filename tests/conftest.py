"""Shared test fixtures: temp DB, test client, auth headers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_tracker.app import create_app
from hermes_tracker.config import Settings
from hermes_tracker.db import init_schema

TEST_TOKEN = "test-secret-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tracker.db"


@pytest.fixture
def connection_factory(db_path: Path) -> Callable[[], sqlite3.Connection]:
    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    with connect() as conn:
        init_schema(conn)
    return connect


@pytest.fixture
def db_conn(
    connection_factory: Callable[[], sqlite3.Connection],
) -> Generator[sqlite3.Connection, None, None]:
    conn = connection_factory()
    yield conn
    conn.close()


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(
        db_path=str(db_path),
        auth_token=TEST_TOKEN,
        cgit_base="https://git.example.org",
    )


@pytest.fixture
def client(
    settings: Settings, connection_factory: Callable[[], sqlite3.Connection]
) -> Generator[TestClient, None, None]:
    """TestClient with one SQLite connection per request."""
    application = create_app(settings=settings, get_conn=connection_factory)
    with TestClient(application) as c:
        yield c
