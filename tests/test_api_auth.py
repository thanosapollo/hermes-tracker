"""Tests for API auth boundaries: unauthenticated mutations are rejected."""

from __future__ import annotations


class TestAuthBoundaries:
    def test_unauthenticated_create_repo_401(self, client) -> None:
        resp = client.post("/api/v1/repos", json={"slug": "x", "name": "X"})
        assert resp.status_code == 401

    def test_unauthenticated_create_todo_401(self, client) -> None:
        resp = client.post("/api/v1/repos/any/todos", json={"title": "T"})
        assert resp.status_code == 401

    def test_wrong_token_401(self, client) -> None:
        resp = client.post(
            "/api/v1/repos",
            json={"slug": "x", "name": "X"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_missing_bearer_prefix_401(self, client) -> None:
        resp = client.post(
            "/api/v1/repos",
            json={"slug": "x", "name": "X"},
            headers={"Authorization": "test-secret-token"},
        )
        assert resp.status_code == 401

    def test_get_repos_requires_auth(self, client) -> None:
        resp = client.get("/api/v1/repos")
        assert resp.status_code == 401
