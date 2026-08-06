"""Fresh exact SQLite schema for the reduced issue ledger."""

from __future__ import annotations

import sqlite3

from .timeutil import utc_now

SCHEMA_VERSION = 9
SCHEMA_SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version=9), applied_at TEXT NOT NULL
);
CREATE TABLE repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    navigation TEXT NOT NULL DEFAULT '[]',
    published INTEGER NOT NULL DEFAULT 1 CHECK(published IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    priority INTEGER NOT NULL DEFAULT 0,
    assignee TEXT,
    author TEXT,
    labels TEXT NOT NULL DEFAULT '[]',
    verification_output TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id,number)
);
CREATE INDEX idx_todos_repo_status_number ON todos(repo_id,status,number);
CREATE TABLE todo_closures (
    todo_id INTEGER PRIMARY KEY REFERENCES todos(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    closed_at TEXT NOT NULL
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_comments_todo_id_id ON comments(todo_id,id);
CREATE TABLE mutation_receipts (
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    mutation_id TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(operation,idempotency_key)
);
CREATE TABLE agent_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_prefix TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    disabled_at TEXT
);
"""


class SchemaVersionError(RuntimeError):
    pass


def _schema_signature(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (row[0], row[1], row[2])
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
               ORDER BY type,name"""
        )
    ]


def _expected_schema_signature() -> list[tuple[str, str, str]]:
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(SCHEMA_SQL)
        return _schema_signature(expected)
    finally:
        expected.close()


EXPECTED_SCHEMA_SIGNATURE = _expected_schema_signature()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if not existing:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (SCHEMA_VERSION, utc_now()),
        )
        conn.commit()
        return
    try:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    except (sqlite3.DatabaseError, TypeError) as exc:
        raise SchemaVersionError(
            "database schema is unsupported; offline migration required"
        ) from exc
    if versions != [SCHEMA_VERSION] or _schema_signature(conn) != EXPECTED_SCHEMA_SIGNATURE:
        raise SchemaVersionError("database schema is unsupported; offline migration required")
