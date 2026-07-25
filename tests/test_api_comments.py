"""Tests for /api/v1 comment endpoints."""

from __future__ import annotations

from conftest import AUTH_HEADERS


def _setup(client) -> None:
    client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
    client.post("/api/v1/repos/proj/todos", json={"title": "T1"}, headers=AUTH_HEADERS)


class TestComments:
    def test_create_comment(self, client) -> None:
        _setup(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/comments",
            json={"author": "agent-1", "body": "Investigating"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["author"] == "agent-1"
        assert data["body"] == "Investigating"

    def test_list_comments(self, client) -> None:
        _setup(client)
        client.post(
            "/api/v1/repos/proj/todos/1/comments",
            json={"author": "a1", "body": "first"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos/1/comments",
            json={"author": "a2", "body": "second"},
            headers=AUTH_HEADERS,
        )
        resp = client.get("/api/v1/repos/proj/todos/1/comments", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_comments_visible_in_todo_detail(self, client) -> None:
        _setup(client)
        client.post(
            "/api/v1/repos/proj/todos/1/comments",
            json={"author": "a1", "body": "found the bug"},
            headers=AUTH_HEADERS,
        )
        resp = client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        comments = resp.json()["comments"]
        assert len(comments) == 1
        assert comments[0]["body"] == "found the bug"
