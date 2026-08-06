"""Lock residual v9 ledger surface contracts for obsolete TODOs #26-#46."""

from __future__ import annotations

import importlib.util

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from tests.test_simple_backend import create_repo, create_todo, keyed

from conftest import AUTH_HEADERS
from hermes_tracker.domain import TodoStatus, normalize_assignee
from hermes_tracker.models import CommentCreate, GitClosureRequest, TodoCreate


def test_todo_status_is_only_open_or_closed() -> None:
    assert {status.value for status in TodoStatus} == {"open", "closed"}


def test_removed_modules_are_absent() -> None:
    for name in (
        "hermes_tracker.mcp",
        "hermes_tracker.commit_scanner",
    ):
        assert importlib.util.find_spec(name) is None


def test_size_bounds_and_assignee_normalization(client: TestClient) -> None:
    create_repo(client)
    assert (
        client.post(
            "/api/v1/repos/source/todos",
            headers=keyed("desc-bound"),
            json={"title": "t", "description": "x" * 20_001},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/repos/source/todos",
            headers=keyed("comment-bound-setup"),
            json={"title": "bound"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/repos/source/todos/1/comments",
            headers=keyed("comment-bound"),
            json={"body": "x" * 20_001},
        ).status_code
        == 422
    )
    assert normalize_assignee("  bob  ") == "bob"
    assert normalize_assignee("   ") is None
    with pytest.raises(ValueError, match="at most 200"):
        normalize_assignee("x" * 201)
    assert (
        client.post(
            "/api/v1/repos/source/todos",
            headers=keyed("assignee-long"),
            json={"title": "a", "assignee": "x" * 201},
        ).status_code
        == 422
    )


def test_comment_and_closure_models_drop_legacy_fields() -> None:
    assert set(CommentCreate.model_fields) == {"body"}
    CommentCreate(body="safe note")
    with pytest.raises(ValidationError):
        CommentCreate(body="safe note", author="spoof")  # type: ignore[call-arg]
    GitClosureRequest(
        source_repo="source",
        source_ref="refs/heads/master",
        commit_sha="a" * 40,
        target_repo="source",
        target_number=1,
    )
    with pytest.raises(ValidationError):
        GitClosureRequest(
            source_repo="source",
            source_ref="refs/heads/master",
            commit_sha="not-hex",
            target_repo="source",
            target_number=1,
        )


def test_api_surface_omits_legacy_mutation_routes(client: TestClient) -> None:
    create_repo(client)
    create_todo(client)
    for path in (
        "/api/v1/repos/source/todos/1/claim",
        "/api/v1/repos/source/todos/1/close",
        "/api/v1/repos/source/todos/1/links",
        "/api/v1/repos/source/todos/1/external-references",
        "/api/v1/repos/source/todos/1/kanban-executions/supersede",
        "/api/v1/events",
        "/mcp",
    ):
        assert client.post(path, headers=AUTH_HEADERS, json={}).status_code in {404, 405}
    properties = client.get("/api/v1/openapi.json", headers=AUTH_HEADERS).json()["components"][
        "schemas"
    ]["TodoCreate"]["properties"]
    assert "acceptance_criteria" not in properties
    assert "verification_commands" not in properties
    assert set(TodoCreate.model_fields) == {
        "title",
        "description",
        "priority",
        "assignee",
        "labels",
    }
