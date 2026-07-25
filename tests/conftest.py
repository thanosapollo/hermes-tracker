"""Shared test fixtures: temp DB, test client, auth headers."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from hermes_tracker.app import create_app
from hermes_tracker.config import Settings
from hermes_tracker.db import init_schema

TEST_TOKEN = "test-secret-token"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def db_conn() -> Generator[sqlite3.Connection, None, None]:
    """In-memory SQLite with schema initialized."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(db_path=":memory:", auth_token=TEST_TOKEN)


@pytest.fixture
def client(settings: Settings, db_conn: sqlite3.Connection) -> Generator[TestClient, None, None]:
    """TestClient with a fresh in-memory DB and auth enabled."""

    def get_conn() -> sqlite3.Connection:
        return db_conn

    application = create_app(settings=settings, get_conn=get_conn)
    with TestClient(application) as c:
        yield c
