"""Tests for SQLite persistence: insert/query repos, todos, comments, links."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_tracker import sqlite_store as store
from hermes_tracker.db import init_schema
from hermes_tracker.domain import LinkError, LinkType


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    return c


@pytest.fixture
def repo(conn: sqlite3.Connection) -> dict:
    return store.insert_repo(conn, slug="test-repo", name="Test Repo")


@pytest.fixture
def todo(conn: sqlite3.Connection, repo: dict) -> dict:
    return store.insert_todo(conn, repo["id"], title="First TODO")


class TestRepoPersistence:
    def test_insert_and_get_repo(self, conn, repo) -> None:
        fetched = store.get_repo(conn, "test-repo")
        assert fetched is not None
        assert fetched["slug"] == "test-repo"
        assert fetched["name"] == "Test Repo"

    def test_get_nonexistent_repo(self, conn) -> None:
        assert store.get_repo(conn, "nope") is None

    def test_list_repos(self, conn) -> None:
        store.insert_repo(conn, slug="a", name="A")
        store.insert_repo(conn, slug="b", name="B")
        repos = store.list_repos(conn)
        assert len(repos) == 2

    def test_duplicate_slug_rejected(self, conn) -> None:
        store.insert_repo(conn, slug="dup", name="First")
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_repo(conn, slug="dup", name="Second")


class TestTodoPersistence:
    def test_insert_and_get_todo(self, conn, repo, todo) -> None:
        fetched = store.get_todo(conn, repo["id"], 1)
        assert fetched is not None
        assert fetched["title"] == "First TODO"
        assert fetched["status"] == "open"
        assert fetched["number"] == 1

    def test_todo_numbers_increment_per_repo(self, conn, repo) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="T1")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        assert t1["number"] == 1
        assert t2["number"] == 2

    def test_todo_number_resets_per_repo(self, conn) -> None:
        r1 = store.insert_repo(conn, slug="r1", name="R1")
        r2 = store.insert_repo(conn, slug="r2", name="R2")
        t1 = store.insert_todo(conn, r1["id"], title="T1")
        t2 = store.insert_todo(conn, r2["id"], title="T2")
        assert t1["number"] == 1
        assert t2["number"] == 1

    def test_todo_default_arrays(self, conn, repo, todo) -> None:
        assert todo["acceptance_criteria"] == []
        assert todo["verification_commands"] == []

    def test_todo_with_criteria_and_commands(self, conn, repo) -> None:
        todo = store.insert_todo(
            conn,
            repo["id"],
            title="T",
            acceptance_criteria=["pass tests", "lint clean"],
            verification_commands=["pytest -q", "ruff check src"],
        )
        assert todo["acceptance_criteria"] == ["pass tests", "lint clean"]
        assert todo["verification_commands"] == ["pytest -q", "ruff check src"]

    def test_update_todo_fields(self, conn, repo, todo) -> None:
        updated = store.update_todo(
            conn,
            todo["id"],
            {
                "title": "Updated Title",
                "priority": 5,
                "assignee": "agent-x",
            },
        )
        assert updated["title"] == "Updated Title"
        assert updated["priority"] == 5
        assert updated["assignee"] == "agent-x"

    def test_update_todo_ignores_protected_fields(self, conn, repo, todo) -> None:
        original_status = todo["status"]
        updated = store.update_todo(conn, todo["id"], {"status": "done"})
        assert updated["status"] == original_status

    def test_list_todos_by_status(self, conn, repo) -> None:
        store.insert_todo(conn, repo["id"], title="T1")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        from hermes_tracker.domain import TodoStatus

        store.set_todo_status(conn, t2["id"], TodoStatus.DONE)
        open_todos = store.list_todos(conn, repo["id"], status="open")
        done_todos = store.list_todos(conn, repo["id"], status="done")
        assert len(open_todos) == 1
        assert len(done_todos) == 1


class TestForeignKeyCascade:
    def test_cascade_delete_repo_deletes_todos(self, conn, repo, todo) -> None:
        todo_id = todo["id"]
        conn.execute("DELETE FROM repositories WHERE id = ?", (repo["id"],))
        conn.commit()
        assert store.get_todo_by_id(conn, todo_id) is None

    def test_cascade_delete_todo_deletes_comments(self, conn, repo, todo) -> None:
        store.insert_comment(conn, todo["id"], "author", "body")
        conn.execute("DELETE FROM todos WHERE id = ?", (todo["id"],))
        conn.commit()
        comments = store.list_comments(conn, todo["id"])
        assert len(comments) == 0


class TestCommentPersistence:
    def test_insert_and_list_comment(self, conn, repo, todo) -> None:
        store.insert_comment(conn, todo["id"], "agent-1", "First comment")
        store.insert_comment(conn, todo["id"], "agent-2", "Second comment")
        comments = store.list_comments(conn, todo["id"])
        assert len(comments) == 2
        assert comments[0]["author"] == "agent-1"
        assert comments[1]["author"] == "agent-2"

    def test_comment_append_only(self, conn, repo, todo) -> None:
        store.insert_comment(conn, todo["id"], "agent-1", "comment")
        comments = store.list_comments(conn, todo["id"])
        # No update function exists; comments are append-only by design
        assert len(comments) == 1


class TestLinkPersistence:
    def test_insert_and_list_link(self, conn, repo) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="T1")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        link = store.insert_link(conn, t1["id"], t2["id"], LinkType.BLOCKS)
        assert link["link_type"] == "blocks"
        links = store.list_links(conn, t1["id"])
        assert len(links) == 1
        assert links[0]["target_number"] == 2
        assert links[0]["target_title"] == "T2"

    def test_self_link_rejected(self, conn, repo, todo) -> None:
        with pytest.raises(LinkError):
            store.insert_link(conn, todo["id"], todo["id"], LinkType.RELATED_TO)

    def test_duplicate_link_rejected(self, conn, repo) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="T1")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        store.insert_link(conn, t1["id"], t2["id"], LinkType.BLOCKS)
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_link(conn, t1["id"], t2["id"], LinkType.BLOCKS)

    def test_has_open_blocking_links_true(self, conn, repo) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="T1")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        store.insert_link(conn, t1["id"], t2["id"], LinkType.DEPENDS_ON)
        assert store.has_open_blocking_links(conn, t1["id"]) is True

    def test_has_open_blocking_links_false_when_target_done(self, conn, repo) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="T1")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        store.insert_link(conn, t1["id"], t2["id"], LinkType.DEPENDS_ON)
        store.close_todo(conn, t2["id"], "done", None)
        assert store.has_open_blocking_links(conn, t1["id"]) is False

    def test_has_open_blocking_links_false_for_non_blocking(self, conn, repo) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="T")
        t2 = store.insert_todo(conn, repo["id"], title="T2")
        store.insert_link(conn, t1["id"], t2["id"], LinkType.RELATED_TO)
        assert store.has_open_blocking_links(conn, t1["id"]) is False


class TestCanonicalLinkSlugs:
    """Forward and reverse link reads must resolve canonical repository aliases.

    The migrated ``agentum-todos`` row keeps its underlying slug but exposes
    ``hermes-tracker`` through ``repository_aliases``. Link queries must not
    leak the legacy slug.
    """

    @pytest.fixture
    def legacy_conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        init_schema(c)
        # Simulate a pre-migration repo row named agentum-todos, then apply the
        # canonical alias exactly as init_schema does for the real legacy DB.
        c.execute(
            "INSERT INTO repositories (slug, name, created_at) "
            "VALUES ('agentum-todos', 'Agentum', '2026-01-01T00:00:00Z')"
        )
        legacy_id = c.execute(
            "SELECT id FROM repositories WHERE slug = 'agentum-todos'"
        ).fetchone()[0]
        c.execute(
            "INSERT INTO repository_aliases (alias_slug, repo_id, is_canonical, created_at) "
            "VALUES ('hermes-tracker', ?, 1, '2026-01-01T00:00:00Z')",
            (legacy_id,),
        )
        c.commit()
        return c

    def test_list_links_returns_canonical_slug_for_legacy_repo(
        self, legacy_conn: sqlite3.Connection
    ) -> None:
        repo = store.get_repo(legacy_conn, "hermes-tracker")
        assert repo is not None
        t1 = store.insert_todo(legacy_conn, repo["id"], title="Source")
        t2 = store.insert_todo(legacy_conn, repo["id"], title="Target")
        store.insert_link(legacy_conn, t1["id"], t2["id"], LinkType.BLOCKS)

        links = store.list_links(legacy_conn, t1["id"])
        assert len(links) == 1
        assert links[0]["target_repo_slug"] == "hermes-tracker"

    def test_list_reverse_links_returns_canonical_slug_for_legacy_repo(
        self, legacy_conn: sqlite3.Connection
    ) -> None:
        repo = store.get_repo(legacy_conn, "hermes-tracker")
        assert repo is not None
        t1 = store.insert_todo(legacy_conn, repo["id"], title="Source")
        t2 = store.insert_todo(legacy_conn, repo["id"], title="Target")
        store.insert_link(legacy_conn, t1["id"], t2["id"], LinkType.BLOCKS)

        reverse = store.list_reverse_links(legacy_conn, t2["id"])
        assert len(reverse) == 1
        assert reverse[0]["source_repo_slug"] == "hermes-tracker"

    def test_list_links_falls_back_to_repo_slug_without_alias(
        self, conn: sqlite3.Connection, repo: dict
    ) -> None:
        t1 = store.insert_todo(conn, repo["id"], title="S")
        t2 = store.insert_todo(conn, repo["id"], title="T")
        store.insert_link(conn, t1["id"], t2["id"], LinkType.RELATED_TO)

        links = store.list_links(conn, t1["id"])
        assert links[0]["target_repo_slug"] == repo["slug"]

        reverse = store.list_reverse_links(conn, t2["id"])
        assert reverse[0]["source_repo_slug"] == repo["slug"]
