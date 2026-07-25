"""Tests for authenticated repository metadata updates."""

from __future__ import annotations

from conftest import AUTH_HEADERS


def test_patch_repo_updates_safe_metadata_and_preserves_todos(client) -> None:
    created = client.post(
        "/api/v1/repos",
        json={"slug": "gnosis", "name": "Old", "description": "Old description"},
        headers=AUTH_HEADERS,
    ).json()
    todo = client.post(
        "/api/v1/repos/gnosis/todos",
        json={"title": "Keep me"},
        headers=AUTH_HEADERS,
    ).json()

    response = client.patch(
        "/api/v1/repos/gnosis",
        json={"name": "Gnosis", "description": "Knowledge system"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        **created,
        "name": "Gnosis",
        "description": "Knowledge system",
    }
    preserved = client.get("/api/v1/repos/gnosis/todos/1", headers=AUTH_HEADERS)
    assert preserved.status_code == 200
    assert preserved.json()["id"] == todo["id"]
    assert preserved.json()["title"] == "Keep me"


def test_patch_repo_requires_auth(client) -> None:
    response = client.patch("/api/v1/repos/gnosis", json={"name": "Gnosis"})

    assert response.status_code == 401


def test_patch_missing_repo_returns_404(client) -> None:
    response = client.patch(
        "/api/v1/repos/missing",
        json={"name": "Missing"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_patch_repo_rejects_null_name(client) -> None:
    client.post(
        "/api/v1/repos",
        json={"slug": "gnosis", "name": "Gnosis"},
        headers=AUTH_HEADERS,
    )

    response = client.patch(
        "/api/v1/repos/gnosis",
        json={"name": None},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422


def test_patch_repo_rejects_fields_outside_safe_metadata(client) -> None:
    client.post(
        "/api/v1/repos",
        json={"slug": "gnosis", "name": "Gnosis"},
        headers=AUTH_HEADERS,
    )

    response = client.patch(
        "/api/v1/repos/gnosis",
        json={"slug": "private-ops"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422
