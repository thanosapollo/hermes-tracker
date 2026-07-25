"""Tests for additive, rollback-compatible SQLite migrations."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from hermes_tracker import db
from hermes_tracker import sqlite_store as store
from hermes_tracker.app import create_app
from hermes_tracker.config import Settings
from hermes_tracker.db import init_schema

LEGACY_SCHEMA = """
CREATE TABLE repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repositories(id),
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    priority INTEGER NOT NULL DEFAULT 0,
    assignee TEXT,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    verification_commands TEXT NOT NULL DEFAULT '[]',
    closing_commit TEXT,
    verification_output TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id, number)
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id INTEGER NOT NULL REFERENCES todos(id),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE todo_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_todo_id INTEGER NOT NULL REFERENCES todos(id),
    target_todo_id INTEGER NOT NULL REFERENCES todos(id),
    link_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_todo_id, target_todo_id, link_type)
);
"""


def test_init_schema_upgrades_populated_legacy_database_idempotently() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO repositories (slug, name, created_at) "
        "VALUES ('agentum-todos', 'Agentum', '2026-01-01T00:00:00Z')"
    )
    repo_id = conn.execute("SELECT id FROM repositories").fetchone()[0]
    conn.execute(
        "INSERT INTO todos (repo_id, number, title, created_at, updated_at) "
        "VALUES (?, 1, 'Keep me', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (repo_id,),
    )
    conn.commit()

    init_schema(conn)
    init_schema(conn)

    assert store.get_repo(conn, "agentum-todos")["slug"] == "hermes-tracker"
    assert store.get_repo(conn, "hermes-tracker")["id"] == repo_id
    legacy_todo = store.get_todo(conn, repo_id, 1)
    assert legacy_todo["title"] == "Keep me"
    assert legacy_todo["author"] is None
    assert legacy_todo["closed_by"] is None
    assert legacy_todo["closed_at"] is None
    assert store.get_repo(conn, "hermes-tracker")["navigation"] == []
    assert store.get_repo(conn, "hermes-tracker")["published"] is True
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "schema_migrations",
        "repository_aliases",
        "external_references",
        "mutation_receipts",
        "superseded_kanban_executions",
    } <= tables
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 1
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 2").fetchone() is not None
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 3").fetchone() is not None
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 4").fetchone() is not None
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 6").fetchone() is not None
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 7").fetchone() is not None
    # An older binary can still locate the unchanged legacy repository row.
    assert (
        conn.execute("SELECT slug FROM repositories WHERE id = ?", (repo_id,)).fetchone()[0]
        == "agentum-todos"
    )

    app = create_app(Settings(db_path=":memory:", auth_token=None), get_conn=lambda: conn)
    with TestClient(app) as client:
        legacy = client.get("/r/agentum-todos", follow_redirects=False)
        canonical = client.get("/r/hermes-tracker")
        duplicate = client.post(
            "/api/v1/repos",
            json={"slug": "hermes-tracker", "name": "Duplicate"},
        )
    assert legacy.status_code == 308
    assert legacy.headers["location"].endswith("/r/hermes-tracker")
    assert canonical.status_code == 200
    assert "Keep me" in canonical.text
    assert duplicate.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 1


def test_closure_migration_backfills_completed_todos() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO repositories (slug, name, created_at) VALUES ('proj', 'Project', 'old')"
    )
    conn.execute(
        "INSERT INTO todos (repo_id, number, title, status, created_at, updated_at) "
        "VALUES (1, 1, 'Done', 'done', 'created', 'completed'), "
        "(1, 2, 'Open', 'open', 'created', 'updated')"
    )

    init_schema(conn)
    init_schema(conn)

    done = store.get_todo(conn, 1, 1)
    open_todo = store.get_todo(conn, 1, 2)
    assert done["closed_at"] == "completed"
    assert done["closed_by"] is None
    assert open_todo["closed_at"] is None


def test_init_schema_upgrades_agent_access_database_with_reused_migration_version() -> None:
    """Upgrade the released agent-access schema that recorded its tables as v4.

    The corrected migration sequence reserves v4 for closure provenance and v5
    for agent access, so this durable pre-correction state must gain the
    closure columns without recreating its existing agent tables.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, sql in db.MIGRATIONS[:3]:
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'old')",
            (version,),
        )
    conn.executescript(
        """
        CREATE TABLE agent_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_prefix TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL,
            scopes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            disabled_at TEXT,
            last_used_at TEXT
        );
        CREATE TABLE tracker_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            actor TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE tracker_event_deliveries (
            event_id INTEGER NOT NULL REFERENCES tracker_events(id) ON DELETE CASCADE,
            consumer TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT,
            acked_at TEXT,
            PRIMARY KEY (event_id, consumer)
        );
        """
    )
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (4, 'old')")
    conn.commit()

    init_schema(conn)
    init_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(todos)")}
    assert {"closed_by", "closed_at"} <= columns
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 5").fetchone()


def test_init_schema_canonicalizes_link_slugs_after_legacy_migration() -> None:
    """After migrating a populated legacy DB, link reads expose the canonical slug.

    Reproduces hermes-tracker#5: ``list_links``/``list_reverse_links`` selected
    ``repositories.slug`` directly, leaking the legacy ``agentum-todos`` slug
    instead of the canonical ``hermes-tracker`` alias.
    """
    from hermes_tracker import sqlite_store as store
    from hermes_tracker.domain import LinkType

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO repositories (slug, name, created_at) "
        "VALUES ('agentum-todos', 'Agentum', '2026-01-01T00:00:00Z')"
    )
    repo_id = conn.execute("SELECT id FROM repositories").fetchone()[0]
    for n in (1, 2):
        conn.execute(
            "INSERT INTO todos (repo_id, number, title, created_at, updated_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (repo_id, n, f"T{n}"),
        )
    conn.commit()

    init_schema(conn)
    t1 = store.get_todo(conn, repo_id, 1)
    t2 = store.get_todo(conn, repo_id, 2)
    store.insert_link(conn, t1["id"], t2["id"], LinkType.RELATED_TO)

    forward = store.list_links(conn, t1["id"])
    assert len(forward) == 1
    assert forward[0]["target_repo_slug"] == "hermes-tracker"

    reverse = store.list_reverse_links(conn, t2["id"])
    assert len(reverse) == 1
    assert reverse[0]["source_repo_slug"] == "hermes-tracker"


def test_failed_migration_rolls_back_its_schema_and_version(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        ((99, "CREATE TABLE partial_write (id INTEGER); INVALID SQL;"),),
    )

    with pytest.raises(sqlite3.OperationalError):
        init_schema(conn)

    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_write'"
        ).fetchone()
        is None
    )
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version = 99").fetchone() is None
