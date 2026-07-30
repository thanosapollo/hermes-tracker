"""SQLite connection helper and explicit additive schema migrations."""

from __future__ import annotations

import sqlite3

from .timeutil import utc_now

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, description TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    number INTEGER NOT NULL, title TEXT NOT NULL, description TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','active','blocked','done')),
    priority INTEGER NOT NULL DEFAULT 0, assignee TEXT,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    verification_commands TEXT NOT NULL DEFAULT '[]', closing_commit TEXT,
    verification_output TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (repo_id, number)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todo_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    target_todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL CHECK (link_type IN (
        'blocks','blocked-by','depends-on','duplicate-of','related-to','supersedes','superseded-by'
    )),
    created_at TEXT NOT NULL,
    UNIQUE (source_todo_id, target_todo_id, link_type),
    CHECK (source_todo_id != target_todo_id)
);
"""

CLOSURE_MIGRATION_SQL = """
ALTER TABLE todos ADD COLUMN closed_by TEXT;
ALTER TABLE todos ADD COLUMN closed_at TEXT;
UPDATE todos SET closed_at = updated_at
WHERE status = 'done' AND closed_at IS NULL;
"""


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE repository_aliases (
            alias_slug TEXT PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
            is_canonical INTEGER NOT NULL DEFAULT 0 CHECK (is_canonical IN (0, 1)),
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX repository_aliases_one_canonical
            ON repository_aliases(repo_id) WHERE is_canonical = 1;
        CREATE TABLE external_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            system TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT,
            metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            UNIQUE (system, external_id)
        );
        CREATE TABLE mutation_receipts (
            idempotency_key TEXT NOT NULL, operation TEXT NOT NULL,
            request_hash TEXT NOT NULL, mutation_id TEXT NOT NULL UNIQUE,
            actor TEXT, status_code INTEGER NOT NULL, response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (operation, idempotency_key)
        );
    """,
    ),
    (2, "ALTER TABLE repositories ADD COLUMN navigation TEXT NOT NULL DEFAULT '[]';"),
    (
        3,
        """
        CREATE TABLE superseded_kanban_executions (
            todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            rationale TEXT NOT NULL,
            replacement_kanban_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (todo_id, task_id)
        );
        """,
    ),
    (4, CLOSURE_MIGRATION_SQL),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS agent_tokens (
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
        CREATE TABLE IF NOT EXISTS tracker_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            actor TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracker_event_deliveries (
            event_id INTEGER NOT NULL REFERENCES tracker_events(id) ON DELETE CASCADE,
            consumer TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT,
            acked_at TEXT,
            PRIMARY KEY (event_id, consumer)
        );
        """,
    ),
    (6, "ALTER TABLE todos ADD COLUMN author TEXT;"),
    (
        7,
        "ALTER TABLE repositories ADD COLUMN published INTEGER NOT NULL DEFAULT 1 "
        "CHECK (published IN (0, 1));",
    ),
    (8, "ALTER TABLE todos ADD COLUMN labels TEXT NOT NULL DEFAULT '[]';"),
)


def connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled and Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the base schema and apply ordered additive migrations."""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    todo_columns = {row[1] for row in conn.execute("PRAGMA table_info(todos)")}
    if 4 in applied and "agent_tokens" in tables and "closed_by" not in todo_columns:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{CLOSURE_MIGRATION_SQL}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    for version, sql in MIGRATIONS:
        if version not in applied:
            try:
                conn.executescript(f"BEGIN IMMEDIATE;\n{sql}")
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    legacy = conn.execute("SELECT id FROM repositories WHERE slug = 'agentum-todos'").fetchone()
    if legacy:
        conn.execute(
            "INSERT OR IGNORE INTO repository_aliases "
            "(alias_slug, repo_id, is_canonical, created_at) VALUES ('hermes-tracker', ?, 1, ?)",
            (legacy[0], utc_now()),
        )
    conn.commit()
