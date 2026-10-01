from __future__ import annotations

import importlib
import json
import sqlite3
import threading

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from hermes_tracker import db, sqlite_store
from hermes_tracker.app import create_app
from hermes_tracker.auth import _scope_for_request, hash_token, token_prefix
from hermes_tracker.config import Settings


def keyed(value: str, headers: dict[str, str] | None = None) -> dict[str, str]:
    return {**(headers or AUTH_HEADERS), "Idempotency-Key": value}


def create_repo(client: TestClient, slug: str = "source") -> None:
    response = client.post(
        "/api/v1/repos", headers=AUTH_HEADERS, json={"slug": slug, "name": slug.title()}
    )
    assert response.status_code == 201


def create_todo(client: TestClient, repo: str = "source", title: str = "Issue") -> dict:
    response = client.post(
        f"/api/v1/repos/{repo}/todos",
        headers=keyed(f"create-{repo}-{title}"),
        json={"title": title, "assignee": "alice", "labels": ["bug"], "priority": 7},
    )
    assert response.status_code == 201, response.text
    return response.json()


def issue(client: TestClient, repo: str = "source", number: int = 1) -> dict:
    response = client.get(f"/api/v1/repos/{repo}/todos/{number}", headers=AUTH_HEADERS)
    assert response.status_code == 200
    return response.json()


def issue_token(factory, token: str, actor: str, scopes: list[str]) -> dict[str, str]:
    with factory() as conn:
        conn.execute(
            "INSERT INTO agent_tokens(token_prefix,actor,token_hash,scopes,created_at) "
            "VALUES(?,?,?,?,?)",
            (token_prefix(token), actor, hash_token(token), json.dumps(scopes), "now"),
        )
        conn.commit()
    return {"Authorization": f"Bearer {token}"}


def closure_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "source_repo": "source",
        "source_ref": "refs/heads/master",
        "commit_sha": "a" * 64,
        "target_repo": "source",
        "target_number": 1,
    }
    body.update(overrides)
    return body


def test_list_repos_includes_summary_open_count(client: TestClient, connection_factory) -> None:
    create_repo(client)
    create_todo(client, title="Open-one")
    create_todo(client, title="Close-me")
    with connection_factory() as conn:
        conn.execute("UPDATE todos SET status='closed' WHERE number=2")
        conn.commit()

    response = client.get("/api/v1/repos", headers=AUTH_HEADERS)

    assert response.status_code == 200
    [repository] = response.json()
    assert repository["summary"] == {
        "status_counts": {"open": 1, "closed": 1},
        "open_count": 1,
        "updated_at": repository["summary"]["updated_at"],
    }
    assert repository["summary"]["updated_at"]


def test_runtime_rejects_same_version_database_with_wrong_reduced_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE schema_migrations(version INTEGER, applied_at TEXT);
        INSERT INTO schema_migrations VALUES(9, 'now');
        CREATE TABLE todos(status TEXT CHECK(status IN ('open','closed')));"""
    )
    with pytest.raises(db.SchemaVersionError, match="offline migration"):
        db.init_schema(conn)


def test_fresh_schema_keeps_only_alias_metadata_from_legacy_surface() -> None:
    conn = sqlite3.connect(":memory:")
    db.init_schema(conn)

    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
        )
    }

    assert names == {
        "agent_tokens",
        "comments",
        "idx_comments_todo_id_id",
        "idx_todos_repo_status_number",
        "mutation_receipts",
        "repositories",
        "repository_aliases",
        "repository_aliases_one_canonical",
        "schema_migrations",
        "todo_closures",
        "todos",
    }


def test_verification_evidence_survives_update_closure_and_reopen(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_todo(client)
    evidence = "pytest -q: 78 passed"
    with connection_factory() as conn:
        conn.execute("UPDATE todos SET verification_output=?", (evidence,))
        conn.commit()

    updated = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("preserve-evidence-update"),
        json={"assignee": "bob"},
    )
    assert updated.json()["verification_output"] == evidence

    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(settings=settings, get_conn=connection_factory)
    with TestClient(app) as scoped:
        closed = scoped.post(
            "/api/v1/git-closures",
            headers=keyed("preserve-evidence-close", hook),
            json=closure_body(),
        )
        page = scoped.get("/r/source/1")
    assert closed.status_code == 200
    assert issue(client)["verification_output"] == evidence
    assert evidence not in page.text

    reopened = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("preserve-evidence-reopen"),
        json={"status": "open"},
    )
    assert reopened.json()["verification_output"] == evidence
    assert reopened.json()["closing_commit"] is None
    properties = client.get("/api/v1/openapi.json", headers=AUTH_HEADERS).json()["components"][
        "schemas"
    ]["TodoResponse"]["properties"]
    assert "verification_output" in properties


def test_issues_start_open_and_ordinary_updates_cannot_close(client: TestClient) -> None:
    create_repo(client)
    created = create_todo(client)
    assert (created["status"], created["closed_at"]) == ("open", None)

    closed = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("manual-close"),
        json={"status": "closed"},
    )
    assert closed.status_code == 422


@pytest.mark.parametrize("status", [None, "active", "blocked", "done", "closed"])
def test_removed_or_null_statuses_are_rejected(client: TestClient, status: object) -> None:
    create_repo(client)
    create_todo(client)
    response = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed(f"bad-{status}"),
        json={"status": status},
    )
    assert response.status_code == 422


def test_assignment_update_does_not_change_status_and_empty_update_fails(
    client: TestClient,
) -> None:
    create_repo(client)
    create_todo(client)
    assigned = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("assign"),
        json={"assignee": "bob"},
    )
    assert (assigned.status_code, assigned.json()["assignee"], assigned.json()["status"]) == (
        200,
        "bob",
        "open",
    )
    empty = client.patch("/api/v1/repos/source/todos/1", headers=keyed("empty"), json={})
    assert empty.status_code == 422
    assert (
        client.patch(
            "/api/v1/repos/source/todos/1",
            headers=keyed("cannot-close"),
            json={"status": "closed"},
        ).status_code
        == 422
    )


def test_title_create_and_update_strip_padding(client: TestClient) -> None:
    create_repo(client)
    created = client.post(
        "/api/v1/repos/source/todos",
        headers=keyed("padded-create"),
        json={"title": "  padded title  "},
    )
    assert created.status_code == 201
    assert created.json()["title"] == "padded title"
    blank_create = client.post(
        "/api/v1/repos/source/todos",
        headers=keyed("blank-create"),
        json={"title": "   "},
    )
    assert blank_create.status_code == 422
    updated = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("padded-update"),
        json={"title": "  next title  "},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "next title"
    blank = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("blank-title"),
        json={"title": "   "},
    )
    assert blank.status_code == 422


def test_closed_todos_reject_field_edits_but_allow_reopen_and_comments(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_todo(client)
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(settings=settings, get_conn=connection_factory)
    with TestClient(app) as scoped:
        assert (
            scoped.post(
                "/api/v1/git-closures", headers=keyed("close-freeze", hook), json=closure_body()
            ).json()["outcome"]
            == "closed"
        )
        detail = scoped.get("/r/source/1")
        head = scoped.head("/r/source/1")
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == str(len(detail.content))

    blocked = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("closed-rewrite"),
        json={"title": "rewritten"},
    )
    assert blocked.status_code == 409
    assert issue(client)["title"] == "Issue"
    mixed = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("closed-mixed"),
        json={"status": "open", "title": "reopened title"},
    )
    assert mixed.status_code == 409
    comment = client.post(
        "/api/v1/repos/source/todos/1/comments",
        headers=keyed("closed-comment"),
        json={"body": "post-close note"},
    )
    assert comment.status_code == 201
    reopened = client.patch(
        "/api/v1/repos/source/todos/1",
        headers=keyed("closed-reopen"),
        json={"status": "open"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closing_commit"] is None


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("GET", "/api/v1/repos", "read"),
        ("POST", "/api/v1/repos", "repo:manage"),
        ("PATCH", "/api/v1/repos/source", "repo:manage"),
        ("POST", "/api/v1/repos/source/todos", "todo:create"),
        ("PATCH", "/api/v1/repos/source/todos/1", "todo:update"),
        ("POST", "/api/v1/repos/source/todos/1/comments", "comment:create"),
        ("POST", "/api/v1/git-closures", "git:close"),
    ],
)
def test_route_scope_contract(method: str, path: str, scope: str) -> None:
    request = Request({"type": "http", "method": method, "path": path, "headers": []})
    assert _scope_for_request(request) == scope


def test_route_scope_strips_root_path() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/todos/api/v1/repos",
            "root_path": "/todos",
            "headers": [],
        }
    )
    assert _scope_for_request(request) == "repo:manage"


def test_missing_or_empty_master_token_never_authenticates_arbitrary_bearer(
    connection_factory, settings: Settings
) -> None:
    reader = issue_token(connection_factory, "htk_read-only-token", "reader", ["read"])
    for master in (None, ""):
        app = create_app(
            settings=Settings(
                db_path=settings.db_path,
                auth_token=master,
                cgit_base=settings.cgit_base,
            ),
            get_conn=connection_factory,
        )
        with TestClient(app) as scoped_client:
            assert (
                scoped_client.get(
                    "/api/v1/repos", headers={"Authorization": "Bearer arbitrary"}
                ).status_code
                == 401
            )
            assert scoped_client.get("/api/v1/repos", headers=reader).status_code == 200
            assert (
                scoped_client.post(
                    "/api/v1/repos",
                    headers=reader,
                    json={"slug": "forbidden", "name": "Forbidden"},
                ).status_code
                == 403
            )


def test_unequal_length_bearer_falls_through_to_scoped_tokens(
    connection_factory, settings: Settings
) -> None:
    short_master = "master-token"
    hook = issue_token(connection_factory, "htk_git-hook-token-long", "git-hook", ["git:close"])
    app = create_app(
        settings=Settings(
            db_path=settings.db_path,
            auth_token=short_master,
            cgit_base="https://git.example.org",
        ),
        get_conn=connection_factory,
    )
    with TestClient(app) as scoped:
        create = scoped.post(
            "/api/v1/repos",
            headers={"Authorization": f"Bearer {short_master}", "Idempotency-Key": "repo"},
            json={"slug": "source", "name": "Source"},
        )
        assert create.status_code == 201
        todo = scoped.post(
            "/api/v1/repos/source/todos",
            headers={"Authorization": f"Bearer {short_master}", "Idempotency-Key": "todo"},
            json={"title": "Close me"},
        )
        assert todo.status_code == 201
        closed = scoped.post(
            "/api/v1/git-closures",
            headers={**hook, "Idempotency-Key": "close-1"},
            json=closure_body(target_repo="source", source_repo="source"),
        )
        assert closed.status_code == 200
        assert closed.json()["outcome"] == "closed"
        assert (
            scoped.post(
                "/api/v1/git-closures",
                headers={
                    "Authorization": f"Bearer {short_master}",
                    "Idempotency-Key": "close-master",
                },
                json=closure_body(target_repo="source", source_repo="source", commit_sha="b" * 40),
            ).status_code
            == 403
        )


def test_search_filters_two_state_views_and_removed_surfaces(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_todo(client, title="Alpha")
    create_todo(client, title="Beta")
    client.patch(
        "/api/v1/repos/source/todos/2",
        headers=keyed("classify"),
        json={"assignee": "bob", "labels": ["feature"]},
    )
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(settings=settings, get_conn=connection_factory)
    with TestClient(app) as closer:
        assert (
            closer.post(
                "/api/v1/git-closures",
                headers=keyed("close-beta", hook),
                json=closure_body(target_number=2),
            ).json()["outcome"]
            == "closed"
        )
    assert [
        x["title"]
        for x in client.get("/api/v1/repos/source/todos?q=alpha", headers=AUTH_HEADERS).json()[
            "todos"
        ]
    ] == ["Alpha"]
    assert [
        x["title"]
        for x in client.get("/api/v1/repos/source/todos?state=closed", headers=AUTH_HEADERS).json()[
            "todos"
        ]
    ] == ["Beta"]
    for suffix in ("claim", "release", "close", "links", "external-references"):
        assert (
            client.post(
                f"/api/v1/repos/source/todos/1/{suffix}", headers=AUTH_HEADERS, json={}
            ).status_code
            == 404
        )
    assert client.get("/api/v1/events", headers=AUTH_HEADERS).status_code == 404


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Fixes #1.", [(None, 1)]),
        ("closed source#2, and other.repo#3", [("source", 2), ("other.repo", 3)]),
        ("RESOLVES #4 and target#5", [(None, 4), ("target", 5)]),
        ("Refs #1\nDone #2\nordinary #3", []),
        ("Fixes #0 and bad slug#4", []),
    ],
)
def test_directive_parser_is_pure(message: str, expected: list[tuple[str | None, int]]) -> None:
    parsed = importlib.import_module("hermes_tracker.commit_directives").parse_directives(message)
    assert [(item.repo_slug, item.number) for item in parsed] == expected


def test_git_closure_authority_provenance_and_idempotency(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_repo(client, "target")
    create_todo(client, "target")
    assert (
        client.post(
            "/api/v1/git-closures", headers=keyed("ordinary"), json=closure_body()
        ).status_code
        == 403
    )
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    settings = Settings(
        db_path=settings.db_path,
        auth_token=settings.auth_token,
        cgit_base="https://git.example.org",
    )
    app = create_app(settings=settings, get_conn=connection_factory)
    body = closure_body(target_repo="target")
    with TestClient(app) as scoped:
        first = scoped.post("/api/v1/git-closures", headers=keyed("same", hook), json=body)
        replay = scoped.post("/api/v1/git-closures", headers=keyed("same", hook), json=body)
        mismatch = scoped.post(
            "/api/v1/git-closures",
            headers=keyed("same", hook),
            json=closure_body(target_repo="target", commit_sha="b" * 64),
        )
        already = scoped.post("/api/v1/git-closures", headers=keyed("other", hook), json=body)
    assert first.json()["outcome"] == "closed"
    assert replay.json() == first.json()
    assert replay.headers["X-Mutation-ID"] == first.headers["X-Mutation-ID"]
    assert mismatch.status_code == 409
    assert already.json()["outcome"] == "already_closed"
    closed = issue(client, "target")
    assert closed["closing_commit"] == "a" * 64
    assert closed["closing_source_repo"] == "source"
    assert closed["closing_source_ref"] == "refs/heads/master"
    assert closed["closed_by"] == "git-hook"
    assert closed["comments"] == []
    with TestClient(app) as public:
        page = public.get("/r/target/1")
    assert page.status_code == 200
    assert "Closed in commit aaaaaaa." in page.text
    assert f"https://git.example.org/source/commit/?id={'a' * 64}" in page.text
    for forbidden in (
        "git-hook closed this",
        "verification",
        "review tree",
        "Kanban",
        "supersession",
        "refs/heads/master",
    ):
        assert forbidden not in page.text


def test_branch_promotion_preserves_first_closure_provenance(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_todo(client)
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(settings=settings, get_conn=connection_factory)
    development = closure_body(source_ref="refs/heads/dev")
    canonical = closure_body(source_ref="refs/heads/master")
    with TestClient(app) as scoped:
        first = scoped.post(
            "/api/v1/git-closures", headers=keyed("development", hook), json=development
        )
        retry = scoped.post(
            "/api/v1/git-closures", headers=keyed("development", hook), json=development
        )
        promotion = scoped.post(
            "/api/v1/git-closures", headers=keyed("canonical", hook), json=canonical
        )
        conflicting_key = scoped.post(
            "/api/v1/git-closures", headers=keyed("development", hook), json=canonical
        )
    assert first.json()["outcome"] == "closed"
    assert retry.headers["X-Mutation-ID"] == first.headers["X-Mutation-ID"]
    assert promotion.json()["outcome"] == "already_closed"
    assert conflicting_key.status_code == 409
    closed = issue(client)
    assert closed["closing_source_ref"] == "refs/heads/dev"
    assert closed["closing_commit"] == development["commit_sha"]
    with connection_factory() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todo_closures").fetchone()[0] == 1


@pytest.mark.parametrize(
    "cgit_base",
    [
        "",
        "http://git.example.org",
        "https://user:pass@git.example.org",
        "https://not a host",
        "https://git.example.org\\evil",
        "https://git.example.org/%zz",
        "https://git.example.org?query=yes",
        "https://git.example.org#fragment",
        "https://-bad.example.org",
        "https://bad-.example.org",
        "https://git.example.org/../cgit",
        "https://git.example.org\n/cgit",
    ],
)
def test_git_closure_fails_closed_without_valid_https_cgit(
    connection_factory, settings: Settings, cgit_base: str
) -> None:
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(
        settings=Settings(
            db_path=settings.db_path,
            auth_token=settings.auth_token,
            cgit_base=cgit_base,
        ),
        get_conn=connection_factory,
    )
    with TestClient(app) as invalid_client:
        create_repo(invalid_client)
        create_todo(invalid_client)
        response = invalid_client.post(
            "/api/v1/git-closures",
            headers=keyed("invalid-cgit", hook),
            json=closure_body(),
        )
    assert response.status_code == 503
    with connection_factory() as conn:
        assert conn.execute("SELECT status FROM todos").fetchone()[0] == "open"
        assert conn.execute("SELECT COUNT(*) FROM todo_closures").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 1


def test_git_closure_rejects_impossible_object_ids_refs_and_source_segments(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_todo(client)
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(settings=settings, get_conn=connection_factory)
    invalid = (
        [{"commit_sha": "a" * length} for length in (39, 41, 63, 65)]
        + [
            {"source_ref": ref}
            for ref in (
                "refs/heads/main..",
                "refs/heads/main.",
                "refs//heads/main",
                "refs/heads/main.lock",
                "refs/heads/.hidden",
                "refs/heads/main~1",
                "refs/heads/main@{upstream}",
            )
        ]
        + [{"source_repo": value} for value in (".", "..")]
    )
    with TestClient(app) as scoped:
        for index, change in enumerate(invalid):
            response = scoped.post(
                "/api/v1/git-closures",
                headers=keyed(f"invalid-provenance-{index}", hook),
                json={**closure_body(), **change},
            )
            assert response.status_code == 422, (change, response.text)
        with connection_factory() as conn:
            assert conn.execute("SELECT status FROM todos").fetchone()[0] == "open"
            assert conn.execute("SELECT COUNT(*) FROM todo_closures").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 1
        valid = scoped.post(
            "/api/v1/git-closures",
            headers=keyed("valid-sha1", hook),
            json=closure_body(commit_sha="b" * 40),
        )
        page = scoped.get("/r/source/1")
    assert valid.json()["outcome"] == "closed"
    assert f"https://git.example.org/source/commit/?id={'b' * 40}" in page.text
    with connection_factory() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todo_closures").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mutation_receipts").fetchone()[0] == 2


def test_reopen_clears_provenance_and_next_git_closure_replaces_comment(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    create_todo(client)
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    settings = Settings(
        db_path=settings.db_path,
        auth_token=settings.auth_token,
        cgit_base="https://git.example.org",
    )
    app = create_app(settings=settings, get_conn=connection_factory)
    with TestClient(app) as scoped:
        assert (
            scoped.post(
                "/api/v1/git-closures", headers=keyed("first", hook), json=closure_body()
            ).json()["outcome"]
            == "closed"
        )
        reopened = scoped.patch(
            "/api/v1/repos/source/todos/1",
            headers=keyed("reopen"),
            json={"status": "open"},
        )
        page_after_reopen = scoped.get("/r/source/1")
        second_body = closure_body(commit_sha="b" * 64)
        assert (
            scoped.post(
                "/api/v1/git-closures", headers=keyed("second", hook), json=second_body
            ).json()["outcome"]
            == "closed"
        )
        final_page = scoped.get("/r/source/1")
    assert reopened.status_code == 200
    assert reopened.json()["closing_commit"] is None
    assert "Closed in commit" not in page_after_reopen.text
    assert "Closed in commit bbbbbbb." in final_page.text
    assert "a" * 64 not in final_page.text
    with connection_factory() as conn:
        assert conn.execute("SELECT COUNT(*) FROM todo_closures").fetchone()[0] == 1


def test_git_closure_missing_and_spoofing_are_safe(
    client: TestClient, connection_factory, settings: Settings
) -> None:
    create_repo(client)
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    app = create_app(settings=settings, get_conn=connection_factory)
    with TestClient(app) as scoped:
        missing = scoped.post(
            "/api/v1/git-closures",
            headers=keyed("missing", hook),
            json=closure_body(target_number=99),
        )
        spoofed = scoped.post(
            "/api/v1/git-closures",
            headers=keyed("spoof", hook),
            json={**closure_body(), "author": "attacker", "comment": "forged"},
        )
    assert missing.json()["outcome"] == "missing"
    assert spoofed.status_code == 422


class _TrackedConnection(sqlite3.Connection):
    closed: bool

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self) -> None:
        super().close()
        self.closed = True


def test_request_connection_factory_closes_startup_and_each_db_request(
    settings: Settings,
) -> None:
    created: list[_TrackedConnection] = []

    def tracked_factory() -> _TrackedConnection:
        connection = sqlite3.connect(
            settings.db_path,
            check_same_thread=False,
            factory=_TrackedConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        created.append(connection)
        return connection

    app = create_app(settings=settings, get_conn=tracked_factory)
    with TestClient(app) as tracked_client:
        assert tracked_client.get("/health").status_code == 200
        create_repo(tracked_client)
        assert tracked_client.get("/").status_code == 200

    assert len(created) == 3
    assert len({id(connection) for connection in created}) == len(created)
    assert all(connection.closed for connection in created)


def test_request_connections_keep_failed_closure_atomic_without_blocking_reads(
    client: TestClient,
    connection_factory,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_repo(client)
    create_todo(client)
    hook = issue_token(connection_factory, "htk_git-hook-token", "git-hook", ["git:close"])
    reader = issue_token(connection_factory, "htk_read-token", "reader", ["read"])
    app = create_app(settings=settings, get_conn=connection_factory)
    comment_started = threading.Event()
    release_comment = threading.Event()
    reader_finished = threading.Event()
    closure_error: list[BaseException] = []

    close_todo = sqlite_store.close_todo_from_git

    def fail_after_closure(*args: object, **kwargs: object) -> None:
        close_todo(*args, **kwargs)
        comment_started.set()
        assert release_comment.wait(5)
        raise sqlite3.IntegrityError("reject closure record")

    monkeypatch.setattr(sqlite_store, "close_todo_from_git", fail_after_closure)

    def close_issue() -> None:
        try:
            with TestClient(app) as closer:
                closer.post(
                    "/api/v1/git-closures",
                    headers=keyed("atomic", hook),
                    json=closure_body(),
                )
        except BaseException as exc:
            closure_error.append(exc)

    def read_issue() -> None:
        with TestClient(app) as read_client:
            response = read_client.get("/api/v1/repos/source/todos/1", headers=reader)
        assert response.status_code == 200
        assert response.json()["status"] == "open"
        reader_finished.set()

    closer_thread = threading.Thread(target=close_issue)
    reader_thread = threading.Thread(target=read_issue)
    closer_thread.start()
    assert comment_started.wait(5)
    reader_thread.start()
    assert reader_finished.wait(2), (
        "reader blocked behind request worker token or write transaction"
    )
    release_comment.set()
    closer_thread.join(5)
    reader_thread.join(5)

    assert not closer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert len(closure_error) == 1
    assert isinstance(closure_error[0], sqlite3.IntegrityError)
    assert issue(client)["status"] == "open"
    with connection_factory() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mutation_receipts WHERE idempotency_key='atomic'"
            ).fetchone()[0]
            == 0
        )
