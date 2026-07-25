"""Regression tests for scoped external-agent credentials."""

from __future__ import annotations

from hermes_tracker import sqlite_store as store
from hermes_tracker.auth import generate_token, hash_token, token_prefix


def _issue_agent(db_conn, *, actor: str, scopes: set[str], expires_at: str | None = None) -> str:
    token = generate_token()
    store.insert_agent_token(
        db_conn,
        actor=actor,
        token_prefix=token_prefix(token),
        token_hash=hash_token(token),
        scopes=scopes,
        expires_at=expires_at,
    )
    return token


def test_agent_identity_is_server_derived_and_scopes_are_enforced(client, db_conn) -> None:
    client.post(
        "/api/v1/repos",
        json={"slug": "demo", "name": "Demo"},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    token = _issue_agent(db_conn, actor="codex", scopes={"read", "comment:create"})
    headers = {"Authorization": f"Bearer {token}", "X-Actor": "forged"}

    forbidden = client.post("/api/v1/repos/demo/todos", json={"title": "Nope"}, headers=headers)
    assert forbidden.status_code == 403

    client.post(
        "/api/v1/repos/demo/todos",
        json={"title": "Allowed"},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    response = client.post(
        "/api/v1/repos/demo/todos/1/comments",
        json={"author": "forged", "body": "I investigated this."},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["author"] == "codex"


def test_todo_update_scope_cannot_change_repository_publication(client, db_conn) -> None:
    master = {"Authorization": "Bearer test-secret-token"}
    client.post("/api/v1/repos", json={"slug": "demo", "name": "Demo"}, headers=master)
    token = _issue_agent(db_conn, actor="editor", scopes={"read", "todo:update"})

    response = client.patch(
        "/api/v1/repos/demo",
        json={"published": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    repo = store.get_repo(db_conn, "demo")
    assert repo is not None
    assert repo["published"] is True


def test_todo_author_is_server_derived_and_payload_cannot_spoof(client, db_conn) -> None:
    client.post(
        "/api/v1/repos",
        json={"slug": "demo", "name": "Demo"},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    token = _issue_agent(db_conn, actor="codex", scopes={"read", "todo:create"})
    headers = {"Authorization": f"Bearer {token}", "X-Actor": "forged"}

    created = client.post("/api/v1/repos/demo/todos", json={"title": "Attributed"}, headers=headers)
    spoofed = client.post(
        "/api/v1/repos/demo/todos",
        json={"title": "Spoofed", "author": "forged"},
        headers=headers,
    )

    assert created.status_code == 201
    assert created.json()["author"] == "codex"
    assert spoofed.status_code == 201
    assert spoofed.json()["author"] == "codex"


def test_kanban_board_identity_backfill_requires_todo_link_scope(client, db_conn) -> None:
    master = {"Authorization": "Bearer test-secret-token"}
    client.post("/api/v1/repos", json={"slug": "demo", "name": "Demo"}, headers=master)
    client.post("/api/v1/repos/demo/todos", json={"title": "T"}, headers=master)
    created = client.post(
        "/api/v1/repos/demo/todos/1/external-references",
        json={"system": "hermes-kanban", "external_id": "t_abcdef12", "metadata": {}},
        headers=master,
    )
    assert created.status_code == 201
    path = (
        f"/api/v1/repos/demo/todos/1/external-references/"
        f"{created.json()['id']}/kanban-board-identity"
    )

    update_only = _issue_agent(db_conn, actor="editor", scopes={"read", "todo:update"})
    forbidden = client.patch(
        path,
        json={"board": "default"},
        headers={"Authorization": f"Bearer {update_only}"},
    )
    assert forbidden.status_code == 403

    linker = _issue_agent(db_conn, actor="linker", scopes={"read", "todo:link"})
    allowed = client.patch(
        path,
        json={"board": "default"},
        headers={"Authorization": f"Bearer {linker}"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["metadata"]["board"] == "default"


def test_revoked_agent_token_is_rejected_and_master_comment_author_is_compatible(
    client, db_conn
) -> None:
    client.post(
        "/api/v1/repos",
        json={"slug": "demo", "name": "Demo"},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    client.post(
        "/api/v1/repos/demo/todos",
        json={"title": "Allowed"},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    token = _issue_agent(db_conn, actor="codex", scopes={"read", "comment:create"})
    assert store.disable_agent_token(db_conn, actor="codex")

    rejected = client.get("/api/v1/repos", headers={"Authorization": f"Bearer {token}"})
    assert rejected.status_code == 401
    compatible = client.post(
        "/api/v1/repos/demo/todos/1/comments",
        json={"author": "legacy-agent", "body": "Legacy attribution."},
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert compatible.status_code == 201
    assert compatible.json()["author"] == "legacy-agent"
