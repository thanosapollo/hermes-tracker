"""Transactional HTTP concurrency and retry contracts, independent of MCP."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from hermes_tracker import sqlite_store
from test_simple_backend import create_repo, create_todo, issue, keyed

PATH = "/api/v1/repos/source/todos/1"


def revision_headers(key: str, revision: str) -> dict[str, str]:
    return {**keyed(key), "If-Match": f'"{revision}"'}


def test_replay_precedes_staleness_and_fingerprints_precondition(client: TestClient) -> None:
    create_repo(client)
    original = create_todo(client)
    first = client.patch(
        PATH, headers=revision_headers("edit", original["revision"]), json={"title": "First"}
    )
    assert first.status_code == 200
    assert first.headers["etag"] == f'"{first.json()["revision"]}"'
    assert first.json()["revision"] != original["revision"]
    second = client.patch(PATH, headers=keyed("legacy-edit"), json={"title": "Second"})
    assert second.status_code == 200  # Existing clients need not send If-Match.
    replay = client.patch(
        PATH, headers=revision_headers("edit", original["revision"]), json={"title": "First"}
    )
    assert replay.json() == first.json()
    assert replay.headers["x-mutation-id"] == first.headers["x-mutation-id"]
    assert replay.headers["etag"] == first.headers["etag"]
    for headers in (keyed("edit"), revision_headers("edit", second.json()["revision"])):
        assert client.patch(PATH, headers=headers, json={"title": "First"}).status_code == 409
    stale = client.patch(
        PATH, headers=revision_headers("stale", original["revision"]), json={"title": "Lost update"}
    )
    assert stale.status_code == 412
    assert issue(client)["title"] == "Second"
    read = client.get(PATH, headers=AUTH_HEADERS)
    assert read.headers["etag"] == f'"{read.json()["revision"]}"'


def test_conditional_replay_distinguishes_omitted_and_explicit_null(client: TestClient) -> None:
    create_repo(client)
    original = create_todo(client)
    headers = revision_headers("patch-presence", original["revision"])
    assert client.patch(PATH, headers=headers, json={"title": "Edited"}).status_code == 200
    assert (
        client.patch(PATH, headers=headers, json={"title": "Edited", "assignee": None}).status_code
        == 409
    )
    assert issue(client)["assignee"] == "alice"


def test_concurrent_edits_have_exactly_one_winner(client: TestClient, connection_factory) -> None:
    create_repo(client)
    revision = create_todo(client)["revision"]
    ready = threading.Barrier(2)

    def edit(index: int) -> int:
        with TestClient(client.app) as writer:
            ready.wait(timeout=5)
            return writer.patch(
                PATH,
                headers=revision_headers(f"race-{index}", revision),
                json={"title": f"Writer {index}"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(edit, range(2))) == [200, 412]
    with connection_factory() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 2


def test_aba_and_clock_rollback_do_not_revive_revision(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(sqlite_store, "utc_now", lambda: "2026-01-01T00:00:00+00:00")
    create_repo(client)
    original = create_todo(client)
    first = client.patch(PATH, headers=keyed("b"), json={"title": "B"})
    monkeypatch.setattr(sqlite_store, "utc_now", lambda: "2025-01-01T00:00:00+00:00")
    back = client.patch(PATH, headers=keyed("a"), json={"title": original["title"]})
    assert first.status_code == back.status_code == 200
    assert original["updated_at"] < first.json()["updated_at"] < back.json()["updated_at"]
    assert back.json()["revision"] != original["revision"]
    assert (
        client.patch(
            PATH, headers=revision_headers("old", original["revision"]), json={"priority": 3}
        ).status_code
        == 412
    )


def test_comment_invalidates_revision_and_replay_does_not(client: TestClient) -> None:
    create_repo(client)
    original = create_todo(client)
    headers = keyed("comment")
    body = {"body": "Public note"}
    first = client.post(PATH + "/comments", headers=headers, json=body)
    current = issue(client)
    assert current["revision"] != original["revision"]
    replay = client.post(PATH + "/comments", headers=headers, json=body)
    assert first.json() == replay.json()
    assert issue(client)["revision"] == current["revision"]
    assert (
        client.patch(
            PATH,
            headers=revision_headers("before-comment", original["revision"]),
            json={"priority": 3},
        ).status_code
        == 412
    )


@pytest.mark.parametrize(
    "value", ["*", "", "abc", 'W/"' + "a" * 64 + '"', '"' + "a" * 64 + '", "' + "b" * 64 + '"']
)
def test_invalid_preconditions_never_become_unconditional(client: TestClient, value: str) -> None:
    create_repo(client)
    create_todo(client)
    assert (
        client.patch(
            PATH, headers={**keyed("bad"), "If-Match": value}, json={"title": "Bad"}
        ).status_code
        == 422
    )
    assert issue(client)["title"] == "Issue"


@pytest.mark.parametrize("operation", ["create", "comment", "update", "reopen"])
def test_receipt_failure_rolls_back_entire_mutation(
    client: TestClient,
    connection_factory,
    monkeypatch,
    operation: str,
) -> None:
    create_repo(client)
    create_todo(client)
    if operation == "reopen":
        with connection_factory() as conn:
            sqlite_store.close_todo_from_git(
                conn,
                1,
                commit_sha="a" * 40,
                source_repo="source",
                source_ref="refs/heads/main",
                actor="hook",
            )
            conn.commit()
    before = issue(client)
    with connection_factory() as conn:
        receipts_before = conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0]

    def fail_receipt(*args, **kwargs):
        raise sqlite3.IntegrityError("injected receipt failure")

    original = sqlite_store.insert_mutation_receipt
    monkeypatch.setattr(sqlite_store, "insert_mutation_receipt", fail_receipt)
    method, path, body = {
        "create": ("POST", PATH.rsplit("/", 1)[0], {"title": "New"}),
        "comment": ("POST", PATH + "/comments", {"body": "Note"}),
        "update": ("PATCH", PATH, {"title": "Edited"}),
        "reopen": ("PATCH", PATH, {"status": "open"}),
    }[operation]
    headers = (
        revision_headers("receipt-fault", before["revision"])
        if method == "PATCH"
        else keyed("receipt-fault")
    )
    with pytest.raises(sqlite3.IntegrityError):
        client.request(method, path, json=body, headers=headers)
    assert issue(client) == before
    with connection_factory() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == receipts_before
        )
    monkeypatch.setattr(sqlite_store, "insert_mutation_receipt", original)
    success = client.request(method, path, json=body, headers=headers)
    replay = client.request(method, path, json=body, headers=headers)
    assert success.status_code in {200, 201}
    assert replay.json() == success.json()
    assert replay.headers["x-mutation-id"] == success.headers["x-mutation-id"]
