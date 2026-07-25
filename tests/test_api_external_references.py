"""Contract tests for bridge-safe external TODO references."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from hermes_tracker import sqlite_store as store
from hermes_tracker.app import create_app
from hermes_tracker.auth import generate_token, hash_token, token_prefix
from hermes_tracker.config import Settings
from hermes_tracker.db import init_schema


def _todo(client) -> None:
    assert (
        client.post(
            "/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/repos/proj/todos", json={"title": "T"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )


def test_create_reference_is_visible_on_todo_and_list(client) -> None:
    _todo(client)
    body = {
        "system": "hermes-kanban",
        "external_id": "t_c23c75a4",
        "url": "https://tracker.invalid/cards/t_c23c75a4",
        "metadata": {"board": "default"},
    }
    created = client.post(
        "/api/v1/repos/proj/todos/1/external-references", json=body, headers=AUTH_HEADERS
    )

    assert created.status_code == 201
    assert created.json() | body == created.json()
    listed = client.get("/api/v1/repos/proj/todos/1/external-references", headers=AUTH_HEADERS)
    detail = client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS)
    assert listed.json() == [created.json()]
    assert detail.json()["external_references"] == [created.json()]


def test_backfill_legacy_kanban_reference_board_preserves_metadata_and_reads_back(client) -> None:
    _todo(client)
    created = client.post(
        "/api/v1/repos/proj/todos/1/external-references",
        json={
            "system": "hermes-kanban",
            "external_id": "t_c23c75a4",
            "metadata": {"profile": "hermes-builder"},
        },
        headers=AUTH_HEADERS,
    )
    assert created.status_code == 201
    path = (
        "/api/v1/repos/proj/todos/1/external-references/"
        f"{created.json()['id']}/kanban-board-identity"
    )

    backfilled = client.patch(path, json={"board": "default"}, headers=AUTH_HEADERS)
    replay = client.patch(path, json={"board": "default"}, headers=AUTH_HEADERS)
    listed = client.get("/api/v1/repos/proj/todos/1/external-references", headers=AUTH_HEADERS)

    assert backfilled.status_code == replay.status_code == 200
    assert backfilled.json() == replay.json()
    assert backfilled.json()["metadata"] == {"board": "default", "profile": "hermes-builder"}
    assert listed.json() == [backfilled.json()]


def test_backfill_rejects_existing_board_and_noncanonical_reference(client) -> None:
    _todo(client)
    canonical = client.post(
        "/api/v1/repos/proj/todos/1/external-references",
        json={
            "system": "hermes-kanban",
            "external_id": "t_c23c75a4",
            "metadata": {"board": "default"},
        },
        headers=AUTH_HEADERS,
    )
    other = client.post(
        "/api/v1/repos/proj/todos/1/external-references",
        json={"system": "kanban", "external_id": "legacy-card"},
        headers=AUTH_HEADERS,
    )
    assert canonical.status_code == other.status_code == 201

    canonical_path = (
        f"/api/v1/repos/proj/todos/1/external-references/{canonical.json()['id']}"
        "/kanban-board-identity"
    )
    other_path = (
        f"/api/v1/repos/proj/todos/1/external-references/{other.json()['id']}/kanban-board-identity"
    )
    conflict = client.patch(canonical_path, json={"board": "other"}, headers=AUTH_HEADERS)
    noncanonical = client.patch(other_path, json={"board": "default"}, headers=AUTH_HEADERS)

    assert conflict.status_code == 409
    assert noncanonical.status_code == 409
    assert client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS).json()[
        "external_references"
    ] == [canonical.json(), other.json()]


def test_close_ignores_generic_external_references(client) -> None:
    """External references are navigation data, not close prerequisites."""
    _todo(client)
    for body in (
        {"system": "hermes-kanban", "external_id": "t_11111111", "metadata": {"board": "gone"}},
        {"system": "other-system", "external_id": "unavailable"},
    ):
        assert (
            client.post(
                "/api/v1/repos/proj/todos/1/external-references", json=body, headers=AUTH_HEADERS
            ).status_code
            == 201
        )

    closed = client.post(
        "/api/v1/repos/proj/todos/1/close",
        json={"verification_output": "pytest -q: passed"},
        headers=AUTH_HEADERS,
    )

    assert closed.status_code == 200
    assert closed.json()["status"] == "done"
    assert len(closed.json()["external_references"]) == 2


def test_external_identity_cannot_attach_to_two_todos(client) -> None:
    _todo(client)
    assert (
        client.post(
            "/api/v1/repos/proj/todos", json={"title": "T2"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )
    body = {"system": "hermes-kanban", "external_id": "t_same"}
    assert (
        client.post(
            "/api/v1/repos/proj/todos/1/external-references", json=body, headers=AUTH_HEADERS
        ).status_code
        == 201
    )

    duplicate = client.post(
        "/api/v1/repos/proj/todos/2/external-references", json=body, headers=AUTH_HEADERS
    )

    assert duplicate.status_code == 409


def _link_kanban_card(client, number: int, task_id: str) -> None:
    assert (
        client.post(
            f"/api/v1/repos/proj/todos/{number}/external-references",
            json={"system": "hermes-kanban", "external_id": task_id},
            headers=AUTH_HEADERS,
        ).status_code
        == 201
    )


def _supersede_path(number: int, task_id: str) -> str:
    return f"/api/v1/repos/proj/todos/{number}/kanban-executions/{task_id}/supersede"


def test_supersede_linked_kanban_execution_is_idempotent_and_visible_on_todo(client) -> None:
    _todo(client)
    _link_kanban_card(client, 1, "t_11111111")
    headers = AUTH_HEADERS | {"Idempotency-Key": "supersede-1", "X-Actor": "agent"}
    body = {
        "rationale": "The implementation moved to the replacement card.",
        "replacement_kanban_id": "t_22222222",
    }
    path = _supersede_path(1, "t_11111111")

    created = client.post(path, json=body, headers=headers)
    replay = client.post(path, json=body, headers=headers)

    assert created.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == created.json()
    assert client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS).json()[
        "superseded_kanban_executions"
    ] == [created.json()]


def test_supersede_receipt_actor_is_server_derived_for_scoped_agent(client, db_conn) -> None:
    _todo(client)
    _link_kanban_card(client, 1, "t_11111111")
    token = generate_token()
    store.insert_agent_token(
        db_conn,
        actor="closer",
        token_prefix=token_prefix(token),
        token_hash=hash_token(token),
        scopes={"read", "todo:close"},
    )
    path = _supersede_path(1, "t_11111111")
    resp = client.post(
        path,
        json={"rationale": "Moved to a replacement card."},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "supersede-actor",
            "X-Actor": "forged",
        },
    )
    assert resp.status_code == 201
    actor = db_conn.execute(
        "SELECT actor FROM mutation_receipts WHERE operation LIKE 'supersede-kanban-execution:%'"
    ).fetchone()[0]
    assert actor == "closer"


def test_supersede_rejects_noncanonical_ids_self_replacement_and_unsafe_rationale(client) -> None:
    _todo(client)
    _link_kanban_card(client, 1, "t_11111111")
    headers = AUTH_HEADERS | {"Idempotency-Key": "invalid-supersede"}

    invalid_path = client.post(
        _supersede_path(1, "t_NOT-HEX"),
        json={"rationale": "Moved to a replacement card."},
        headers=headers,
    )
    invalid_replacement = client.post(
        _supersede_path(1, "t_11111111"),
        json={"rationale": "Moved to a replacement card.", "replacement_kanban_id": "t_UPPER123"},
        headers=headers,
    )
    self_replacement = client.post(
        _supersede_path(1, "t_11111111"),
        json={"rationale": "Moved to a replacement card.", "replacement_kanban_id": "t_11111111"},
        headers=headers,
    )
    unsafe_rationale = client.post(
        _supersede_path(1, "t_11111111"),
        json={"rationale": "See /home/user/private.log"},
        headers=headers,
    )

    assert [
        invalid_path.status_code,
        invalid_replacement.status_code,
        self_replacement.status_code,
        unsafe_rationale.status_code,
    ] == [422, 422, 422, 422]
    detail = client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS).json()
    assert detail["superseded_kanban_executions"] == []


def test_supersede_rejects_unlinked_or_foreign_kanban_cards_without_persistence(client) -> None:
    _todo(client)
    assert (
        client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Other"},
            headers=AUTH_HEADERS,
        ).status_code
        == 201
    )
    _link_kanban_card(client, 2, "t_22222222")
    body = {"rationale": "Moved to a replacement card."}

    unlinked = client.post(_supersede_path(1, "t_11111111"), json=body, headers=AUTH_HEADERS)
    foreign = client.post(_supersede_path(1, "t_22222222"), json=body, headers=AUTH_HEADERS)

    assert unlinked.status_code == foreign.status_code == 409
    assert (
        client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS).json()[
            "superseded_kanban_executions"
        ]
        == []
    )


def test_supersede_replay_mismatch_and_duplicate_write_leave_atomic_state(client, db_conn) -> None:
    _todo(client)
    _link_kanban_card(client, 1, "t_11111111")
    path = _supersede_path(1, "t_11111111")
    headers = AUTH_HEADERS | {"Idempotency-Key": "supersede-atomic", "X-Actor": "agent"}
    body = {"rationale": "Moved to a replacement card."}

    created = client.post(path, json=body, headers=headers)
    mismatch = client.post(path, json={"rationale": "Different reason."}, headers=headers)
    duplicate = client.post(
        path,
        json=body,
        headers=AUTH_HEADERS | {"Idempotency-Key": "supersede-distinct-key"},
    )

    assert created.status_code == 201
    assert mismatch.status_code == duplicate.status_code == 409
    detail = client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS).json()
    assert len(detail["superseded_kanban_executions"]) == 1
    assert (
        db_conn.execute(
            "SELECT COUNT(*) FROM mutation_receipts WHERE operation = ?",
            ("supersede-kanban-execution:1:t_11111111",),
        ).fetchone()[0]
        == 1
    )


def test_concurrent_supersede_replay_records_one_execution_and_receipt(tmp_path) -> None:
    db_path = tmp_path / "tracker.sqlite"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        init_schema(connection)
        return connection

    settings = Settings(db_path=db_path, auth_token="test-secret-token")
    first_connection = connect()
    second_connection = connect()
    first_app = create_app(settings=settings, get_conn=lambda: first_connection)
    second_app = create_app(settings=settings, get_conn=lambda: second_connection)
    path = _supersede_path(1, "t_11111111")
    headers = AUTH_HEADERS | {"Idempotency-Key": "concurrent-supersede"}
    body = {"rationale": "Moved to a replacement card."}

    try:
        with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
            _todo(first_client)
            _link_kanban_card(first_client, 1, "t_11111111")
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(
                    executor.map(
                        lambda active_client: active_client.post(path, json=body, headers=headers),
                        (first_client, second_client),
                    )
                )
    finally:
        first_connection.close()
        second_connection.close()

    assert [response.status_code for response in responses] == [201, 201]
    with sqlite3.connect(db_path) as connection:
        execution_count = connection.execute(
            "SELECT COUNT(*) FROM superseded_kanban_executions"
        ).fetchone()[0]
        assert execution_count == 1
        assert connection.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 1
