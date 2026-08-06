"""Tests for application-level deployment behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hermes_tracker.app import create_app
from hermes_tracker.config import Settings


def test_health_is_public_and_minimal(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_path_drives_private_docs_openapi_and_generated_links(
    settings: Settings, connection_factory
) -> None:
    settings = Settings(
        db_path=settings.db_path,
        auth_token=settings.auth_token,
        root_path="/todos",
        cgit_base=settings.cgit_base,
    )
    application = create_app(settings=settings, get_conn=connection_factory)
    headers = {"Authorization": f"Bearer {settings.auth_token}"}

    with TestClient(application, base_url="http://testserver", root_path="/todos") as client:
        created = client.post(
            "/api/v1/repos",
            headers=headers,
            json={"slug": "project", "name": "Project"},
        )
        homepage = client.get("/")
        docs = client.get("/api/v1/docs", headers=headers)
        schema = client.get("/api/v1/openapi.json", headers=headers)

    assert created.status_code == 201
    assert homepage.status_code == 200
    assert 'href="http://testserver/todos/"' in homepage.text
    assert 'href="http://testserver/todos/r/project"' in homepage.text
    assert docs.status_code == 200
    assert "url: '/todos/api/v1/openapi.json'" in docs.text
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "Hermes Tracker"
    assert schema.json()["servers"] == [{"url": "/todos"}]
    assert schema.json()["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
    }
    assert set(schema.json()["paths"]) == {
        "/api/v1/labels",
        "/api/v1/repos",
        "/api/v1/repos/{slug}",
        "/api/v1/repos/{slug}/todos",
        "/api/v1/repos/{slug}/todos/{number}",
        "/api/v1/repos/{slug}/todos/{number}/comments",
        "/api/v1/git-closures",
    }
    for path in schema.json()["paths"].values():
        for operation in path.values():
            assert operation["security"] == [{"HTTPBearer": []}]
