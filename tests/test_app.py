"""Tests for application-level deployment behavior."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from hermes_tracker.app import create_app
from hermes_tracker.config import Settings


def test_health_is_public_and_minimal(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_path_drives_docs_openapi_and_generated_links(db_conn: sqlite3.Connection) -> None:
    settings = Settings(db_path=":memory:", auth_token=None, root_path="/todos")
    application = create_app(settings=settings, get_conn=lambda: db_conn)

    with TestClient(application, base_url="http://testserver", root_path="/todos") as client:
        created = client.post(
            "/api/v1/repos",
            json={"slug": "project", "name": "Project"},
        )
        homepage = client.get("/")
        docs = client.get("/docs")
        schema = client.get("/openapi.json")

    assert created.status_code == 201
    assert homepage.status_code == 200
    assert 'href="http://testserver/todos/"' in homepage.text
    assert 'href="http://testserver/todos/r/project"' in homepage.text
    assert docs.status_code == 200
    assert "url: '/todos/openapi.json'" in docs.text
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "Hermes Tracker"
    assert schema.json()["servers"] == [{"url": "/todos"}]
