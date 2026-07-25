"""Contract tests for idempotent bridge mutations."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from conftest import AUTH_HEADERS
from hermes_tracker import sqlite_store as store


def _headers(key: str) -> dict[str, str]:
    return {**AUTH_HEADERS, "Idempotency-Key": key, "X-Actor": "agent"}


def _repo(client) -> None:
    assert (
        client.post(
            "/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )


def _todo(client) -> None:
    _repo(client)
    assert (
        client.post(
            "/api/v1/repos/proj/todos", json={"title": "T"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )


def assert_replay(first, replay, expected_status: int) -> None:
    assert first.status_code == replay.status_code == expected_status
    assert first.json() == replay.json()
    assert first.headers["X-Mutation-ID"] == replay.headers["X-Mutation-ID"]


def test_create_todo_replay_does_not_allocate_another_number(client) -> None:
    _repo(client)
    first = client.post(
        "/api/v1/repos/proj/todos", json={"title": "T"}, headers=_headers("create-1")
    )
    replay = client.post(
        "/api/v1/repos/proj/todos", json={"title": "T"}, headers=_headers("create-1")
    )

    assert_replay(first, replay, 201)
    assert len(client.get("/api/v1/repos/proj/todos", headers=AUTH_HEADERS).json()["todos"]) == 1


@pytest.mark.parametrize(
    ("path", "body", "count_path"),
    [
        (
            "/api/v1/repos/proj/todos/1/comments",
            {"author": "agent", "body": "note"},
            "/api/v1/repos/proj/todos/1/comments",
        ),
        (
            "/api/v1/repos/proj/todos/1/links",
            {"target_repo_slug": "proj", "target_number": 2, "link_type": "related-to"},
            "/api/v1/repos/proj/todos/1/links",
        ),
    ],
)
def test_append_mutation_replay_does_not_duplicate(client, path, body, count_path) -> None:
    _todo(client)
    assert (
        client.post(
            "/api/v1/repos/proj/todos", json={"title": "T2"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )
    first = client.post(path, json=body, headers=_headers("append-1"))
    replay = client.post(path, json=body, headers=_headers("append-1"))

    assert_replay(first, replay, 201)
    assert len(client.get(count_path, headers=AUTH_HEADERS).json()) == 1


@pytest.mark.parametrize(
    ("suffix", "body", "status"),
    [
        ("claim", {"assignee": "worker"}, 200),
        ("close", {"verification_output": "tests pass"}, 200),
    ],
)
def test_transition_replay_returns_original_success(client, suffix, body, status) -> None:
    _todo(client)
    path = f"/api/v1/repos/proj/todos/1/{suffix}"
    first = client.post(path, json=body, headers=_headers(f"transition-{suffix}"))
    replay = client.post(path, json=body, headers=_headers(f"transition-{suffix}"))

    assert_replay(first, replay, status)


def test_same_key_with_different_input_is_conflict(client) -> None:
    _repo(client)
    assert (
        client.post(
            "/api/v1/repos/proj/todos", json={"title": "First"}, headers=_headers("same-key")
        ).status_code
        == 201
    )

    conflict = client.post(
        "/api/v1/repos/proj/todos", json={"title": "Different"}, headers=_headers("same-key")
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Idempotency key was already used with different input"


def test_successful_unkeyed_mutation_still_exposes_mutation_id(client) -> None:
    _repo(client)
    response = client.post("/api/v1/repos/proj/todos", json={"title": "T"}, headers=AUTH_HEADERS)
    assert response.status_code == 201
    assert response.headers["X-Mutation-ID"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/repos/proj/todos/1/claim", {"assignee": "worker"}),
        ("/api/v1/repos/proj/todos/1/close", {"verification_output": "tests pass"}),
    ],
)
def test_transition_reloads_todo_after_write_transaction_begins(
    client, monkeypatch, path, body
) -> None:
    _todo(client)
    original_get_todo = store.get_todo

    def require_transaction(conn, *args, **kwargs):
        assert conn.in_transaction
        return original_get_todo(conn, *args, **kwargs)

    monkeypatch.setattr(store, "get_todo", require_transaction)

    response = client.post(path, json=body, headers=_headers("transaction-state"))

    assert response.status_code == 200


def test_concurrent_close_with_distinct_keys_preserves_first_closure_evidence(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "tracker.sqlite"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        from hermes_tracker.db import init_schema

        init_schema(connection)
        return connection

    from fastapi.testclient import TestClient

    from hermes_tracker.app import create_app
    from hermes_tracker.config import Settings

    first_connection = connect()
    second_connection = connect()
    settings = Settings(db_path=db_path, auth_token="test-secret-token")
    first_app = create_app(settings=settings, get_conn=lambda: first_connection)
    second_app = create_app(settings=settings, get_conn=lambda: second_connection)
    pre_transaction_reads = Barrier(2)
    original_get_todo = store.get_todo

    def synchronize_stale_reads(conn, *args, **kwargs):
        if not conn.in_transaction:
            position = pre_transaction_reads.wait(timeout=2)
            assert position in (0, 1)
        return original_get_todo(conn, *args, **kwargs)

    monkeypatch.setattr(store, "get_todo", synchronize_stale_reads)

    try:
        with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
            _todo(first_client)
            path = "/api/v1/repos/proj/todos/1/close"
            requests = (
                (
                    first_client,
                    {"verification_output": "first evidence"},
                    _headers("close-first"),
                ),
                (
                    second_client,
                    {"verification_output": "second evidence"},
                    _headers("close-second"),
                ),
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(client.post, path, json=body, headers=headers)
                    for client, body, headers in requests
                ]
                responses = [future.result(timeout=5) for future in futures]
    finally:
        first_connection.close()
        second_connection.close()

    assert sorted(response.status_code for response in responses) == [200, 409]
    successful_response = next(response for response in responses if response.status_code == 200)
    with sqlite3.connect(db_path) as connection:
        todo = connection.execute(
            "SELECT status, verification_output, closed_by, closed_at, updated_at "
            "FROM todos WHERE id = 1"
        ).fetchone()
        assert todo[:3] == (
            "done",
            successful_response.json()["verification_output"],
            "agent",
        )
        assert todo[3] == todo[4] == successful_response.json()["closed_at"]
        assert connection.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 1


def test_concurrent_write_cannot_commit_side_effect_when_receipt_insert_fails(
    client, db_conn, monkeypatch
) -> None:
    _repo(client)
    side_effect_ready = Event()

    def fail_receipt(*args, **kwargs) -> None:
        side_effect_ready.set()
        raise RuntimeError("injected receipt failure")

    monkeypatch.setattr(store, "insert_mutation_receipt", fail_receipt)

    with ThreadPoolExecutor(max_workers=2) as executor:
        covered = executor.submit(
            client.post,
            "/api/v1/repos/proj/todos",
            json={"title": "must roll back"},
            headers=_headers("receipt-fails"),
        )
        assert side_effect_ready.wait(timeout=2)
        concurrent = executor.submit(
            client.patch,
            "/api/v1/repos/proj",
            json={"description": "concurrent write"},
            headers=AUTH_HEADERS,
        )

        with pytest.raises(RuntimeError, match="injected receipt failure"):
            covered.result(timeout=2)
        assert concurrent.result(timeout=2).status_code == 200

    assert db_conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 0
    repo = store.get_repo(db_conn, "proj")
    assert repo is not None
    assert repo["description"] == "concurrent write"
