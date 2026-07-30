"""Tests for the /api/v1 TODO endpoints: create/list/get/update/claim/close."""

from __future__ import annotations

from conftest import AUTH_HEADERS


def _create_repo(client, slug: str = "proj", name: str = "Project") -> dict:
    resp = client.post("/api/v1/repos", json={"slug": slug, "name": name}, headers=AUTH_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_todo(client, slug: str = "proj", **fields) -> dict:
    body = {"title": "Test TODO", **fields}
    resp = client.post(f"/api/v1/repos/{slug}/todos", json=body, headers=AUTH_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestRepoEndpoints:
    def test_repository_navigation_round_trips_and_is_in_openapi(self, client) -> None:
        navigation = [
            {"label": "summary", "url": "https://git.thanosapollo.org/proj"},
            {"label": "todos", "url": "/r/proj"},
        ]
        created = client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "navigation": navigation},
            headers=AUTH_HEADERS,
        )
        assert created.status_code == 201
        assert created.json()["navigation"] == navigation
        listed = client.get("/api/v1/repos", headers=AUTH_HEADERS).json()
        assert listed[0]["navigation"] == navigation
        properties = client.get("/openapi.json").json()["components"]["schemas"]["RepoResponse"][
            "properties"
        ]
        assert "navigation" in properties

    def test_repository_navigation_rejects_unsafe_urls(self, client) -> None:
        response = client.post(
            "/api/v1/repos",
            json={
                "slug": "proj",
                "name": "Project",
                "navigation": [{"label": "summary", "url": "javascript:alert(1)"}],
            },
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 422

    def test_create_repo(self, client) -> None:
        resp = client.post(
            "/api/v1/repos",
            json={"slug": "my-repo", "name": "My Repo"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == "my-repo"
        assert data["name"] == "My Repo"

    def test_create_duplicate_repo_409(self, client) -> None:
        _create_repo(client)
        resp = client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Dup"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 409

    def test_list_repos(self, client) -> None:
        _create_repo(client, slug="a", name="A")
        _create_repo(client, slug="b", name="B")
        resp = client.get("/api/v1/repos", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestTodoCreateGetList:
    def test_create_todo(self, client) -> None:
        _create_repo(client)
        resp = client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Fix bug", "priority": 3, "acceptance_criteria": ["tests pass"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Fix bug"
        assert data["number"] == 1
        assert data["status"] == "open"
        assert data["priority"] == 3
        assert data["acceptance_criteria"] == ["tests pass"]

    def test_create_get_and_list_include_authenticated_author(self, client) -> None:
        _create_repo(client)
        created = client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Attributed"},
            headers={**AUTH_HEADERS, "X-Actor": "agent@example.org"},
        )

        assert created.status_code == 201
        assert created.json()["author"] == "agent@example.org"
        assert (
            client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS).json()["author"]
            == "agent@example.org"
        )
        assert (
            client.get("/api/v1/repos/proj/todos", headers=AUTH_HEADERS).json()["todos"][0][
                "author"
            ]
            == "agent@example.org"
        )

    def test_get_todo(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="Find me")
        resp = client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["title"] == "Find me"

    def test_get_nonexistent_todo_404(self, client) -> None:
        _create_repo(client)
        resp = client.get("/api/v1/repos/proj/todos/999", headers=AUTH_HEADERS)
        assert resp.status_code == 404

    def test_list_todos(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="T1")
        _create_todo(client, title="T2")
        resp = client.get("/api/v1/repos/proj/todos?state=all", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["todos"]) == 2
        assert data["repo"]["slug"] == "proj"

    def test_list_todos_filter_by_status(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="T1")
        # claim the first todo to make it active
        client.post(
            "/api/v1/repos/proj/todos/1/claim",
            json={"assignee": "a1"},
            headers=AUTH_HEADERS,
        )
        _create_todo(client, title="T2")
        resp = client.get("/api/v1/repos/proj/todos?status_filter=open", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()["todos"]) == 1

    def test_list_todos_rejects_invalid_status_filter(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="T1")
        invalid = client.get(
            "/api/v1/repos/proj/todos?status_filter=in+review", headers=AUTH_HEADERS
        )
        valid = client.get("/api/v1/repos/proj/todos?status_filter=open", headers=AUTH_HEADERS)
        unfiltered = client.get("/api/v1/repos/proj/todos?state=all", headers=AUTH_HEADERS)
        assert invalid.status_code == 422
        assert valid.status_code == 200
        assert unfiltered.status_code == 200
        assert len(valid.json()["todos"]) == 1
        assert len(unfiltered.json()["todos"]) == 1

    def test_list_todos_defaults_to_open_state(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="Open item")
        client.post(
            "/api/v1/repos/proj/todos/1/claim",
            json={"assignee": "worker"},
            headers=AUTH_HEADERS,
        )
        _create_todo(client, title="Still open")
        done = _create_todo(client, title="Finished")
        client.post(
            f"/api/v1/repos/proj/todos/{done['number']}/close",
            json={"verification_output": "ok"},
            headers=AUTH_HEADERS,
        )
        default = client.get("/api/v1/repos/proj/todos", headers=AUTH_HEADERS)
        assert default.status_code == 200
        titles = {todo["title"] for todo in default.json()["todos"]}
        assert titles == {"Open item", "Still open"}
        closed = client.get("/api/v1/repos/proj/todos?state=closed", headers=AUTH_HEADERS)
        assert {todo["title"] for todo in closed.json()["todos"]} == {"Finished"}
        all_states = client.get("/api/v1/repos/proj/todos?state=all", headers=AUTH_HEADERS)
        assert len(all_states.json()["todos"]) == 3

    def test_list_todos_search_and_label_assignee_filters(self, client) -> None:
        _create_repo(client)
        _create_todo(
            client,
            title="Fix Auth Login",
            labels=["bug", "auth"],
            assignee="alice",
        )
        _create_todo(
            client,
            title="Docs pass",
            labels=["docs"],
            assignee="bob",
        )
        _create_todo(
            client,
            title="Auth hardening",
            labels=["auth", "security"],
            assignee="alice",
        )
        by_q = client.get("/api/v1/repos/proj/todos?q=auth", headers=AUTH_HEADERS)
        assert {todo["title"] for todo in by_q.json()["todos"]} == {
            "Fix Auth Login",
            "Auth hardening",
        }
        by_labels = client.get(
            "/api/v1/repos/proj/todos?label=auth&label=bug", headers=AUTH_HEADERS
        )
        assert [todo["title"] for todo in by_labels.json()["todos"]] == ["Fix Auth Login"]
        by_assignees = client.get(
            "/api/v1/repos/proj/todos?assignee=alice&assignee=carol",
            headers=AUTH_HEADERS,
        )
        assert {todo["title"] for todo in by_assignees.json()["todos"]} == {
            "Fix Auth Login",
            "Auth hardening",
        }
        assert by_labels.json()["todos"][0]["labels"] == ["auth", "bug"]

    def test_list_todos_rejects_invalid_filters(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="T1")
        assert (
            client.get("/api/v1/repos/proj/todos?state=maybe", headers=AUTH_HEADERS).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/repos/proj/todos?q=" + ("x" * 201), headers=AUTH_HEADERS
            ).status_code
            == 422
        )
        assert (
            client.get("/api/v1/repos/proj/todos?label=Bad Label", headers=AUTH_HEADERS).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/repos/proj/todos?" + "&".join(f"label=l{i}" for i in range(17)),
                headers=AUTH_HEADERS,
            ).status_code
            == 422
        )

    def test_create_get_and_list_include_labels(self, client) -> None:
        _create_repo(client)
        created = client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Labeled", "labels": ["Bug", " auth ", "bug"]},
            headers=AUTH_HEADERS,
        )
        assert created.status_code == 201
        assert created.json()["labels"] == ["auth", "bug"]
        detail = client.get("/api/v1/repos/proj/todos/1", headers=AUTH_HEADERS)
        assert detail.json()["labels"] == ["auth", "bug"]
        listed = client.get("/api/v1/repos/proj/todos", headers=AUTH_HEADERS)
        assert listed.json()["todos"][0]["labels"] == ["auth", "bug"]
        updated = client.patch(
            "/api/v1/repos/proj/todos/1",
            json={"labels": ["docs"]},
            headers=AUTH_HEADERS,
        )
        assert updated.status_code == 200
        assert updated.json()["labels"] == ["docs"]

    def test_create_rejects_invalid_labels(self, client) -> None:
        _create_repo(client)
        resp = client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Nope", "labels": ["not valid!"]},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422


class TestTodoUpdate:
    def test_update_title_and_priority(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        resp = client.patch(
            "/api/v1/repos/proj/todos/1",
            json={"title": "Updated", "priority": 10},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated"
        assert data["priority"] == 10

    def test_update_assignee(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        resp = client.patch(
            "/api/v1/repos/proj/todos/1",
            json={"assignee": "agent-7"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["assignee"] == "agent-7"


class TestClaim:
    def test_claim_open_todo(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/claim",
            json={"assignee": "worker-1"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["assignee"] == "worker-1"

    def test_reclaim_active_todo(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        client.post(
            "/api/v1/repos/proj/todos/1/claim",
            json={"assignee": "worker-1"},
            headers=AUTH_HEADERS,
        )
        resp = client.post(
            "/api/v1/repos/proj/todos/1/claim",
            json={"assignee": "worker-2"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["assignee"] == "worker-2"


class TestClose:
    def test_close_with_verification(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "all tests passed", "closing_commit": "abc123"},
            headers={**AUTH_HEADERS, "X-Actor": "codex"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "done"
        assert data["verification_output"] == "all tests passed"
        assert data["closing_commit"] == "abc123"
        assert data["closed_by"] == "codex"
        assert data["closed_at"] == data["updated_at"]

    def test_close_accepts_full_sha256_commit(self, client) -> None:
        _create_repo(client)
        _create_todo(client)

        response = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "directive", "closing_commit": "a" * 64},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["closing_commit"] == "a" * 64

    def test_actorless_close_remains_valid(self, client) -> None:
        _create_repo(client)
        _create_todo(client)

        data = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "non-code work complete"},
            headers=AUTH_HEADERS,
        ).json()

        assert data["closed_by"] is None
        assert data["closed_at"]

    def test_close_without_verification_409(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": ""},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422  # pydantic min_length validation

    def test_close_already_done_409(self, client) -> None:
        _create_repo(client)
        _create_todo(client)
        client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "done"},
            headers=AUTH_HEADERS,
        )
        resp = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "again"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 409

    def test_close_blocked_by_dependency_409(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="T1")
        _create_todo(client, title="T2")
        # T1 depends-on T2; T2 is not done → cannot close T1
        client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "depends-on"},
            headers=AUTH_HEADERS,
        )
        resp = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "ok"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 409
        assert "blocked" in resp.json()["detail"].lower()

    def test_close_after_dependency_resolved(self, client) -> None:
        _create_repo(client)
        _create_todo(client, title="T1")
        _create_todo(client, title="T2")
        # T1 depends-on T2
        client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "depends-on"},
            headers=AUTH_HEADERS,
        )
        # Close T2 first
        client.post(
            "/api/v1/repos/proj/todos/2/close",
            json={"verification_output": "done"},
            headers=AUTH_HEADERS,
        )
        # Now T1 can be closed
        resp = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "ok"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"
