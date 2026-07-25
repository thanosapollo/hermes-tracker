"""Tests for /api/v1 link endpoints."""

from __future__ import annotations

from conftest import AUTH_HEADERS
from hermes_tracker import sqlite_store as store
from hermes_tracker.auth import generate_token, hash_token, token_prefix
from hermes_tracker.domain import LinkType


def _setup(client, n: int = 2) -> None:
    client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
    for _ in range(n):
        client.post("/api/v1/repos/proj/todos", json={"title": "T"}, headers=AUTH_HEADERS)


class TestLinks:
    def test_create_link(self, client) -> None:
        _setup(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "blocks"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["link_type"] == "blocks"
        assert data["target_number"] == 2

    def test_list_links(self, client) -> None:
        _setup(client, n=3)
        client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "blocks"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 3, "link_type": "related-to"},
            headers=AUTH_HEADERS,
        )
        resp = client.get("/api/v1/repos/proj/todos/1/links", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_self_link_rejected(self, client) -> None:
        _setup(client, n=1)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 1, "link_type": "related-to"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code in (409, 422)

    def test_invalid_link_type_rejected(self, client) -> None:
        _setup(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "nonsense"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    def test_create_link_receipt_actor_is_server_derived_for_scoped_agent(
        self, client, db_conn
    ) -> None:
        _setup(client)
        token = generate_token()
        store.insert_agent_token(
            db_conn,
            actor="linker",
            token_prefix=token_prefix(token),
            token_hash=hash_token(token),
            scopes={"read", "todo:link"},
        )
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "related-to"},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "link-actor",
                "X-Actor": "forged",
            },
        )
        assert resp.status_code == 201
        actor = db_conn.execute(
            "SELECT actor FROM mutation_receipts WHERE operation LIKE 'create-link:%'"
        ).fetchone()[0]
        assert actor == "linker"

    def test_create_link_master_keeps_supplied_actor(self, client, db_conn) -> None:
        _setup(client)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 2, "link_type": "related-to"},
            headers={
                **AUTH_HEADERS,
                "Idempotency-Key": "link-master-actor",
                "X-Actor": "agent",
            },
        )
        assert resp.status_code == 201
        actor = db_conn.execute(
            "SELECT actor FROM mutation_receipts WHERE operation LIKE 'create-link:%'"
        ).fetchone()[0]
        assert actor == "agent"

    def test_unknown_target_todo_404(self, client) -> None:
        _setup(client, n=1)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "proj", "target_number": 999, "link_type": "related-to"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404

    def test_cross_repo_link(self, client) -> None:
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        client.post("/api/v1/repos", json={"slug": "other", "name": "Other"}, headers=AUTH_HEADERS)
        client.post("/api/v1/repos/proj/todos", json={"title": "T1"}, headers=AUTH_HEADERS)
        client.post("/api/v1/repos/other/todos", json={"title": "T2"}, headers=AUTH_HEADERS)
        resp = client.post(
            "/api/v1/repos/proj/todos/1/links",
            json={"target_repo_slug": "other", "target_number": 1, "link_type": "related-to"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 201
        assert resp.json()["target_repo_slug"] == "other"


class TestCanonicalLinkResponses:
    """API link responses must expose canonical repository slugs.

    When a repo row keeps a legacy slug (e.g. ``agentum-todos``) but has a
    canonical alias (``hermes-tracker``), link reads must report the canonical
    slug, never the legacy one.
    """

    def test_forward_link_response_uses_canonical_slug(self, client, db_conn) -> None:
        # Seed a legacy repo row with a canonical alias, mirroring a migrated DB.
        db_conn.execute(
            "INSERT INTO repositories (slug, name, created_at) "
            "VALUES ('agentum-todos', 'Agentum', '2026-01-01T00:00:00Z')"
        )
        legacy_id = db_conn.execute(
            "SELECT id FROM repositories WHERE slug = 'agentum-todos'"
        ).fetchone()[0]
        db_conn.execute(
            "INSERT INTO repository_aliases (alias_slug, repo_id, is_canonical, created_at) "
            "VALUES ('hermes-tracker', ?, 1, '2026-01-01T00:00:00Z')",
            (legacy_id,),
        )
        db_conn.commit()

        t1 = store.insert_todo(db_conn, legacy_id, title="Source")
        t2 = store.insert_todo(db_conn, legacy_id, title="Target")
        store.insert_link(db_conn, t1["id"], t2["id"], LinkType.BLOCKS)

        resp = client.get("/api/v1/repos/hermes-tracker/todos/1/links", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["target_repo_slug"] == "hermes-tracker"
