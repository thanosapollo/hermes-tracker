"""Migration tests for Hermes Tracker's persisted data."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient


def test_canonical_package_exposes_application() -> None:
    from hermes_tracker.app import create_app
    from hermes_tracker.config import Settings

    application = create_app(
        settings=Settings(db_path=":memory:", auth_token=None),
        get_conn=lambda: sqlite3.connect(":memory:"),
    )

    assert application.title == "Hermes Tracker"


def test_existing_database_remains_readable_after_runtime_migration(tmp_path) -> None:
    from hermes_tracker.app import create_app
    from hermes_tracker.config import Settings
    from hermes_tracker.db import init_schema

    database = tmp_path / "existing.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    init_schema(connection)
    connection.execute(
        "INSERT INTO repositories (slug, name, created_at) VALUES (?, ?, ?)",
        ("preserved", "Preserved repository", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    application = create_app(Settings(db_path=str(database), auth_token=None))
    with TestClient(application) as client:
        response = client.get("/r/preserved")

    assert response.status_code == 200
    assert "Preserved repository" in response.text
