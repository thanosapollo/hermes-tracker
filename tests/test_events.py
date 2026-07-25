"""Tests for the durable Tracker event pull/ack contract."""

from __future__ import annotations

from hermes_tracker import sqlite_store as store
from hermes_tracker.auth import generate_token, hash_token, token_prefix


def _issue_agent(db_conn, *, actor: str, scopes: set[str]) -> str:
    token = generate_token()
    store.insert_agent_token(
        db_conn,
        actor=actor,
        token_prefix=token_prefix(token),
        token_hash=hash_token(token),
        scopes=scopes,
    )
    return token


def test_selected_events_are_delivered_per_consumer_and_acknowledged(client) -> None:
    headers = {"Authorization": "Bearer test-secret-token", "X-Actor": "worker"}
    assert (
        client.post(
            "/api/v1/repos", json={"slug": "demo", "name": "Demo"}, headers=headers
        ).status_code
        == 201
    )
    created = client.post(
        "/api/v1/repos/demo/todos",
        json={"title": "Wake", "assignee": "worker"},
        headers=headers,
    )
    assert created.status_code == 201
    assert (
        client.patch(
            "/api/v1/repos/demo/todos/1", json={"priority": 2}, headers=headers
        ).status_code
        == 200
    )

    first = client.get("/api/v1/events?consumer=plugin-a", headers=headers)
    assert first.status_code == 200
    events = first.json()["events"]
    assert [event["event_type"] for event in events] == ["todo.assigned"]
    event_id = events[0]["id"]

    other_consumer = client.get("/api/v1/events?consumer=plugin-b", headers=headers)
    assert [event["id"] for event in other_consumer.json()["events"]] == [event_id]
    assert client.post(
        f"/api/v1/events/{event_id}/ack?consumer=plugin-a", headers=headers
    ).json() == {"acknowledged": True}
    assert client.get("/api/v1/events?consumer=plugin-a", headers=headers).json()["events"] == []
    assert client.post(
        f"/api/v1/events/{event_id}/ack?consumer=plugin-a", headers=headers
    ).json() == {"acknowledged": True}


def test_tracker_mention_enqueues_comment_event(client) -> None:
    headers = {"Authorization": "Bearer test-secret-token", "X-Actor": "worker"}
    client.post("/api/v1/repos", json={"slug": "demo", "name": "Demo"}, headers=headers)
    client.post("/api/v1/repos/demo/todos", json={"title": "Wake"}, headers=headers)
    assert (
        client.post(
            "/api/v1/repos/demo/todos/1/comments",
            json={"author": "worker", "body": "Please inspect this, @tracker."},
            headers=headers,
        ).status_code
        == 201
    )

    events = client.get("/api/v1/events?consumer=plugin", headers=headers).json()["events"]
    assert [event["event_type"] for event in events] == ["todo.comment.requested"]


def test_claim_and_close_events_use_the_authenticated_agent_actor(client, db_conn) -> None:
    master_headers = {"Authorization": "Bearer test-secret-token"}
    client.post("/api/v1/repos", json={"slug": "demo", "name": "Demo"}, headers=master_headers)
    client.post("/api/v1/repos/demo/todos", json={"title": "Wake"}, headers=master_headers)
    token = _issue_agent(db_conn, actor="codex", scopes={"read", "todo:claim", "todo:close"})
    agent_headers = {"Authorization": f"Bearer {token}", "X-Actor": "forged"}

    claimed = client.post(
        "/api/v1/repos/demo/todos/1/claim",
        json={"assignee": "codex"},
        headers=agent_headers,
    )
    assert claimed.status_code == 200
    closed = client.post(
        "/api/v1/repos/demo/todos/1/close",
        json={"verification_output": "passed"},
        headers=agent_headers,
    )
    assert closed.status_code == 200
    assert closed.json()["closed_by"] == "codex"
    repo = store.get_repo(db_conn, "demo")
    assert repo is not None
    todo = store.get_todo(db_conn, repo["id"], 1)
    assert todo is not None
    assert todo["closed_by"] == "codex"

    events = client.get("/api/v1/events?consumer=plugin", headers=agent_headers).json()["events"]
    assert [(event["event_type"], event["actor"]) for event in events] == [
        ("todo.claimed", "codex"),
        ("todo.closed", "codex"),
    ]
