"""Declared legacy v8 schema accepted by offline conversion tests and validation."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

V8_SCHEMA_SQL = """
CREATE TABLE repositories (
 id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 description TEXT, created_at TEXT NOT NULL, navigation TEXT NOT NULL DEFAULT '[]',
 published INTEGER NOT NULL DEFAULT 1 CHECK(published IN (0,1))
);
CREATE TABLE todos (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
 number INTEGER NOT NULL, title TEXT NOT NULL, description TEXT,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','active','blocked','done')),
 priority INTEGER NOT NULL DEFAULT 0, assignee TEXT,
 acceptance_criteria TEXT NOT NULL DEFAULT '[]', verification_commands TEXT NOT NULL DEFAULT '[]',
 closing_commit TEXT, verification_output TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 closed_by TEXT, closed_at TEXT, author TEXT, labels TEXT NOT NULL DEFAULT '[]',
 UNIQUE(repo_id,number)
);
CREATE TABLE comments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
 author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE todo_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
 target_todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
 link_type TEXT NOT NULL CHECK(link_type IN (
  'blocks','blocked-by','depends-on','duplicate-of','related-to','supersedes','superseded-by'
 )),
 created_at TEXT NOT NULL, UNIQUE(source_todo_id,target_todo_id,link_type),
 CHECK(source_todo_id != target_todo_id)
);
CREATE TABLE repository_aliases (
 alias_slug TEXT PRIMARY KEY,
 repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
 is_canonical INTEGER NOT NULL DEFAULT 0 CHECK(is_canonical IN (0,1)), created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX repository_aliases_one_canonical
 ON repository_aliases(repo_id) WHERE is_canonical=1;
CREATE TABLE external_references (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
 system TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT, metadata TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, UNIQUE(system,external_id)
);
CREATE TABLE mutation_receipts (
 idempotency_key TEXT NOT NULL, operation TEXT NOT NULL, request_hash TEXT NOT NULL,
 mutation_id TEXT NOT NULL UNIQUE, actor TEXT, status_code INTEGER NOT NULL,
 response_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(operation,idempotency_key)
);
CREATE TABLE superseded_kanban_executions (
 todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
 task_id TEXT NOT NULL, rationale TEXT NOT NULL,
 replacement_kanban_id TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(todo_id,task_id)
);
CREATE TABLE agent_tokens (
 id INTEGER PRIMARY KEY AUTOINCREMENT, token_prefix TEXT NOT NULL UNIQUE,
 actor TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL, scopes TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT, disabled_at TEXT, last_used_at TEXT
);
CREATE TABLE tracker_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
 todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
 actor TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE tracker_event_deliveries (
 event_id INTEGER NOT NULL REFERENCES tracker_events(id) ON DELETE CASCADE,
 consumer TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 delivered_at TEXT, acked_at TEXT, PRIMARY KEY(event_id,consumer)
);
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
"""


def structural_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    """Fingerprint complete normalized DDL plus SQLite structural metadata."""
    objects = [
        (row[0], row[1], row[2], _schema_tokens(row[3]))
        for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
        )
    ]
    tables = [row[1] for row in objects if row[0] == "table" and row[1] != "sqlite_sequence"]
    return {
        "objects": objects,
        "columns": {
            table: [tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in tables
        },
        "foreign_keys": {
            table: [tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({table})")]
            for table in tables
        },
        "indexes": {
            table: {
                row[1]: [tuple(column) for column in conn.execute(f"PRAGMA index_xinfo({row[1]})")]
                for row in conn.execute(f"PRAGMA index_list({table})")
            }
            for table in tables
        },
    }


def _schema_tokens(sql: str) -> tuple[str, ...]:
    """Canonicalize formatting while retaining constraints and index semantics."""
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z0-9_]*|"
        r"0[xX][0-9A-Fa-f]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?|<=|>=|<>|!=|\S",
        sql,
    )
    canonical = [token if token.startswith(("'", '"')) else token.lower() for token in tokens]
    if canonical[:5] in (
        ["create", "table", "if", "not", "exists"],
        ["create", "index", "if", "not", "exists"],
    ):
        del canonical[2:5]
    return tuple(canonical)


def expected_v8_fingerprint() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(V8_SCHEMA_SQL)
        return structural_fingerprint(conn)
    finally:
        conn.close()


EXPECTED_V8_FINGERPRINT = expected_v8_fingerprint()
