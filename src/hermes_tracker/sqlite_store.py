"""SQLite store: small functions for repos, todos, comments, and links.

The store layer handles all SQL. It encodes/decodes JSON-array columns
(acceptance_criteria, verification_commands) and returns plain dicts.
Domain logic (status transitions, link validation) lives in domain.py and is
called by the API layer, not here.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .domain import LinkError, LinkType, TodoStatus
from .timeutil import utc_now

# ---------------------------------------------------------------------------
# JSON-array helpers
# ---------------------------------------------------------------------------


def _encode_list(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _decode_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in result] if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Row-to-dict converters
# ---------------------------------------------------------------------------


def _decode_navigation(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [
        {"label": item["label"], "url": item["url"]}
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("label"), str)
        and isinstance(item.get("url"), str)
    ]


def _repo_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "navigation": _decode_navigation(row["navigation"]),
        "published": bool(row["published"]),
        "created_at": row["created_at"],
    }


def _todo_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "repo_id": row["repo_id"],
        "number": row["number"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee": row["assignee"],
        "author": row["author"],
        "acceptance_criteria": _decode_list(row["acceptance_criteria"]),
        "verification_commands": _decode_list(row["verification_commands"]),
        "closing_commit": row["closing_commit"],
        "verification_output": row["verification_output"],
        "closed_by": row["closed_by"],
        "closed_at": row["closed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def insert_repo(
    conn: sqlite3.Connection,
    slug: str,
    name: str,
    description: str | None = None,
    navigation: list[dict[str, str]] | None = None,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    now = utc_now()
    conn.execute(
        """INSERT INTO repositories (slug, name, description, navigation, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (slug, name, description, json.dumps(navigation or []), now),
    )
    _maybe_commit(conn, commit)
    return get_repo(conn, slug)  # type: ignore[return-value]


def _canonical_repo_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = _repo_row(row)
    alias = conn.execute(
        "SELECT alias_slug FROM repository_aliases WHERE repo_id = ? AND is_canonical = 1",
        (row["id"],),
    ).fetchone()
    if alias:
        result["slug"] = alias["alias_slug"]
    return result


def get_repo(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT DISTINCT r.* FROM repositories r
           LEFT JOIN repository_aliases a ON a.repo_id = r.id
           WHERE r.slug = ? OR a.alias_slug = ?""",
        (slug, slug),
    ).fetchone()
    return _canonical_repo_row(conn, row) if row else None


def get_repo_by_id(conn: sqlite3.Connection, repo_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM repositories WHERE id = ?", (repo_id,)).fetchone()
    return _canonical_repo_row(conn, row) if row else None


def list_repos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM repositories ORDER BY created_at").fetchall()
    return [_canonical_repo_row(conn, row) for row in rows]


def list_published_repos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM repositories WHERE published = 1 ORDER BY created_at"
    ).fetchall()
    return [_canonical_repo_row(conn, row) for row in rows]


def _maybe_commit(conn: sqlite3.Connection, commit: bool) -> None:
    if commit:
        conn.commit()


def repo_summary(conn: sqlite3.Connection, repo_id: int) -> dict[str, Any]:
    """Return terse tracker metadata for a repository.

    Provides counts and timestamps the index/list views show alongside the
    repository name and description. The shape matches cgit's IDLE column in
    spirit: only earned data, never fabricated.
    """
    open_count = conn.execute(
        "SELECT COUNT(*) AS n FROM todos WHERE repo_id = ? AND status != 'done'",
        (repo_id,),
    ).fetchone()["n"]
    updated = conn.execute(
        "SELECT MAX(updated_at) AS t FROM todos WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()["t"]
    return {"open_count": open_count, "updated_at": updated}


def list_repos_with_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List repos with per-repo summary stats for the index table."""
    repos = list_repos(conn)
    for repo in repos:
        repo["summary"] = repo_summary(conn, repo["id"])
    return repos


def list_published_repos_with_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List publicly visible repos with per-repo summary stats."""
    repos = list_published_repos(conn)
    for repo in repos:
        repo["summary"] = repo_summary(conn, repo["id"])
    return repos


def update_repo(
    conn: sqlite3.Connection,
    repo_id: int,
    fields: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, Any] | None:
    allowed = {"name", "description", "navigation", "published"}
    updates = [(key, value) for key, value in fields.items() if key in allowed]
    if not updates:
        return get_repo_by_id(conn, repo_id)
    assignments = ", ".join(f"{key} = ?" for key, _ in updates)
    conn.execute(
        f"UPDATE repositories SET {assignments} WHERE id = ?",  # noqa: S608
        [json.dumps(value) if key == "navigation" else value for key, value in updates] + [repo_id],
    )
    _maybe_commit(conn, commit)
    return get_repo_by_id(conn, repo_id)


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------


def _next_todo_number(conn: sqlite3.Connection, repo_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 AS next FROM todos WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()
    return int(row["next"])


def insert_todo(
    conn: sqlite3.Connection,
    repo_id: int,
    title: str,
    description: str | None = None,
    priority: int = 0,
    assignee: str | None = None,
    acceptance_criteria: list[str] | None = None,
    verification_commands: list[str] | None = None,
    author: str | None = None,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    now = utc_now()
    number = _next_todo_number(conn, repo_id)
    conn.execute(
        """INSERT INTO todos
             (repo_id, number, title, description, status, priority, assignee,
              acceptance_criteria, verification_commands,
              closing_commit, verification_output, created_at, updated_at, author)
           VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
        (
            repo_id,
            number,
            title,
            description,
            priority,
            assignee,
            _encode_list(acceptance_criteria or []),
            _encode_list(verification_commands or []),
            now,
            now,
            author,
        ),
    )
    _maybe_commit(conn, commit)
    return get_todo(conn, repo_id, number)  # type: ignore[return-value]


def get_todo(conn: sqlite3.Connection, repo_id: int, number: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM todos WHERE repo_id = ? AND number = ?",
        (repo_id, number),
    ).fetchone()
    return _todo_row(row) if row else None


def get_todo_by_id(conn: sqlite3.Connection, todo_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_row(row) if row else None


def list_todos(
    conn: sqlite3.Connection,
    repo_id: int,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            """SELECT * FROM todos WHERE repo_id = ? AND status = ?
               ORDER BY number""",
            (repo_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM todos WHERE repo_id = ? ORDER BY number",
            (repo_id,),
        ).fetchall()
    return [_todo_row(r) for r in rows]


def update_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    fields: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, Any] | None:
    """Update mutable fields on a TODO.

    Only title, description, priority, assignee, acceptance_criteria,
    verification_commands are accepted. Status, number, repo_id, closing
    fields are managed by dedicated transitions.
    """
    allowed = {
        "title",
        "description",
        "priority",
        "assignee",
        "acceptance_criteria",
        "verification_commands",
    }
    updates: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("acceptance_criteria", "verification_commands"):
            value = _encode_list(value or [])
        updates.append(f"{key} = ?")
        values.append(value)
    if not updates:
        return get_todo_by_id(conn, todo_id)
    updates.append("updated_at = ?")
    values.append(utc_now())
    values.append(todo_id)
    conn.execute(
        f"UPDATE todos SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
        values,
    )
    _maybe_commit(conn, commit)
    return get_todo_by_id(conn, todo_id)


def set_todo_status(
    conn: sqlite3.Connection, todo_id: int, status: TodoStatus, *, commit: bool = True
) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE todos SET status = ?, updated_at = ? WHERE id = ?",
        (status.value, utc_now(), todo_id),
    )
    _maybe_commit(conn, commit)
    return get_todo_by_id(conn, todo_id)


def set_todo_assignee(
    conn: sqlite3.Connection, todo_id: int, assignee: str, *, commit: bool = True
) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE todos SET assignee = ?, updated_at = ? WHERE id = ?",
        (assignee, utc_now(), todo_id),
    )
    _maybe_commit(conn, commit)
    return get_todo_by_id(conn, todo_id)


def close_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    verification_output: str,
    closing_commit: str | None,
    closed_by: str | None = None,
    *,
    commit: bool = True,
) -> dict[str, Any] | None:
    closed_at = utc_now()
    conn.execute(
        """UPDATE todos
           SET status = 'done',
               verification_output = ?,
               closing_commit = ?,
               closed_by = ?,
               closed_at = ?,
               updated_at = ?
           WHERE id = ?""",
        (verification_output, closing_commit, closed_by, closed_at, closed_at, todo_id),
    )
    _maybe_commit(conn, commit)
    return get_todo_by_id(conn, todo_id)


# ---------------------------------------------------------------------------
# Comments (append-only)
# ---------------------------------------------------------------------------


def insert_comment(
    conn: sqlite3.Connection,
    todo_id: int,
    author: str,
    body: str,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    now = utc_now()
    cur = conn.execute(
        """INSERT INTO comments (todo_id, author, body, created_at)
           VALUES (?, ?, ?, ?)""",
        (todo_id, author, body, now),
    )
    _maybe_commit(conn, commit)
    return {
        "id": cur.lastrowid,
        "todo_id": todo_id,
        "author": author,
        "body": body,
        "created_at": now,
    }


def list_comments(conn: sqlite3.Connection, todo_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM comments WHERE todo_id = ? ORDER BY created_at",
        (todo_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "todo_id": r["todo_id"],
            "author": r["author"],
            "body": r["body"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def insert_link(
    conn: sqlite3.Connection,
    source_todo_id: int,
    target_todo_id: int,
    link_type: LinkType,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    if source_todo_id == target_todo_id:
        raise LinkError("A TODO cannot link to itself")
    now = utc_now()
    cur = conn.execute(
        """INSERT INTO todo_links (source_todo_id, target_todo_id, link_type, created_at)
           VALUES (?, ?, ?, ?)""",
        (source_todo_id, target_todo_id, link_type.value, now),
    )
    _maybe_commit(conn, commit)
    return {
        "id": cur.lastrowid,
        "source_todo_id": source_todo_id,
        "target_todo_id": target_todo_id,
        "link_type": link_type.value,
        "created_at": now,
    }


def list_links(conn: sqlite3.Connection, todo_id: int) -> list[dict[str, Any]]:
    """List links where this todo is the source."""
    rows = conn.execute(
        """SELECT tl.*, t.number AS target_number,
                  t.title AS target_title,
                  COALESCE(a.alias_slug, r.slug) AS target_repo_slug
           FROM todo_links tl
           JOIN todos t ON tl.target_todo_id = t.id
           JOIN repositories r ON t.repo_id = r.id
           LEFT JOIN repository_aliases a
             ON a.repo_id = r.id AND a.is_canonical = 1
           WHERE tl.source_todo_id = ?
           ORDER BY tl.created_at""",
        (todo_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "link_type": r["link_type"],
            "target_todo_id": r["target_todo_id"],
            "target_number": r["target_number"],
            "target_title": r["target_title"],
            "target_repo_slug": r["target_repo_slug"],
        }
        for r in rows
    ]


def has_open_blocking_links(conn: sqlite3.Connection, todo_id: int) -> bool:
    """Check if a todo has unresolved blocked-by or depends-on links.

    A blocking link is 'open' when its target todo is NOT done.
    """
    rows = conn.execute(
        """SELECT tl.target_todo_id
           FROM todo_links tl
           JOIN todos t ON tl.target_todo_id = t.id
           WHERE tl.source_todo_id = ?
             AND tl.link_type IN ('blocked-by', 'depends-on')
             AND t.status != 'done'""",
        (todo_id,),
    ).fetchall()
    return len(rows) > 0


def list_reverse_links(conn: sqlite3.Connection, todo_id: int) -> list[dict[str, Any]]:
    """List links where this todo is the target (reverse links for display)."""
    rows = conn.execute(
        """SELECT tl.*, s.number AS source_number,
                  s.title AS source_title,
                  COALESCE(a.alias_slug, r.slug) AS source_repo_slug
           FROM todo_links tl
           JOIN todos s ON tl.source_todo_id = s.id
           JOIN repositories r ON s.repo_id = r.id
           LEFT JOIN repository_aliases a
             ON a.repo_id = r.id AND a.is_canonical = 1
           WHERE tl.target_todo_id = ?
           ORDER BY tl.created_at""",
        (todo_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "link_type": r["link_type"],
            "source_todo_id": r["source_todo_id"],
            "source_number": r["source_number"],
            "source_title": r["source_title"],
            "source_repo_slug": r["source_repo_slug"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# External references and idempotency receipts
# ---------------------------------------------------------------------------


def insert_external_reference(
    conn: sqlite3.Connection,
    todo_id: int,
    system: str,
    external_id: str,
    url: str | None,
    metadata: dict[str, str],
    *,
    commit: bool = True,
) -> dict[str, Any]:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO external_references "
        "(todo_id, system, external_id, url, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (todo_id, system, external_id, url, json.dumps(metadata, sort_keys=True), now),
    )
    _maybe_commit(conn, commit)
    return {
        "id": cursor.lastrowid,
        "system": system,
        "external_id": external_id,
        "url": url,
        "metadata": metadata,
        "created_at": now,
    }


def list_external_references(conn: sqlite3.Connection, todo_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM external_references WHERE todo_id = ? ORDER BY id", (todo_id,)
    ).fetchall()
    return [
        {
            "id": row["id"],
            "system": row["system"],
            "external_id": row["external_id"],
            "url": row["url"],
            "metadata": json.loads(row["metadata"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_external_reference(
    conn: sqlite3.Connection, todo_id: int, reference_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM external_references WHERE todo_id = ? AND id = ?", (todo_id, reference_id)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "system": row["system"],
        "external_id": row["external_id"],
        "url": row["url"],
        "metadata": json.loads(row["metadata"]),
        "created_at": row["created_at"],
    }


def backfill_external_reference_board_identity(
    conn: sqlite3.Connection, reference_id: int, metadata: dict[str, str]
) -> None:
    conn.execute(
        "UPDATE external_references SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, sort_keys=True), reference_id),
    )


def supersede_kanban_execution(
    conn: sqlite3.Connection,
    todo_id: int,
    task_id: str,
    rationale: str,
    replacement_kanban_id: str | None,
) -> dict[str, Any]:
    created_at = utc_now()
    conn.execute(
        "INSERT INTO superseded_kanban_executions "
        "(todo_id, task_id, rationale, replacement_kanban_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (todo_id, task_id, rationale, replacement_kanban_id, created_at),
    )
    return {
        "task_id": task_id,
        "rationale": rationale,
        "replacement_kanban_id": replacement_kanban_id,
        "created_at": created_at,
    }


def list_superseded_kanban_executions(
    conn: sqlite3.Connection, todo_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT task_id, rationale, replacement_kanban_id, created_at "
        "FROM superseded_kanban_executions WHERE todo_id = ? ORDER BY created_at, task_id",
        (todo_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_mutation_receipt(
    conn: sqlite3.Connection, operation: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM mutation_receipts WHERE operation = ? AND idempotency_key = ?",
        (operation, idempotency_key),
    ).fetchone()
    return dict(row) if row else None


def insert_mutation_receipt(
    conn: sqlite3.Connection,
    *,
    operation: str,
    idempotency_key: str,
    request_hash: str,
    mutation_id: str,
    actor: str | None,
    status_code: int,
    response_json: str,
) -> None:
    conn.execute(
        "INSERT INTO mutation_receipts "
        "(operation, idempotency_key, request_hash, mutation_id, actor, status_code, "
        "response_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation,
            idempotency_key,
            request_hash,
            mutation_id,
            actor,
            status_code,
            response_json,
            utc_now(),
        ),
    )


# ---------------------------------------------------------------------------
# External-agent credentials and durable event outbox
# ---------------------------------------------------------------------------


def insert_agent_token(
    conn: sqlite3.Connection,
    *,
    actor: str,
    token_prefix: str,
    token_hash: str,
    scopes: set[str],
    expires_at: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Persist one credential verifier; raw credentials never enter this layer."""
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO agent_tokens (token_prefix, actor, token_hash, scopes, created_at, "
        "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_prefix, actor, token_hash, json.dumps(sorted(scopes)), now, expires_at),
    )
    _maybe_commit(conn, commit)
    return {
        "id": cursor.lastrowid,
        "actor": actor,
        "token_prefix": token_prefix,
        "scopes": sorted(scopes),
    }


def list_active_agent_tokens(
    conn: sqlite3.Connection, token_prefix: str, now: str
) -> list[dict[str, Any]]:
    """Return viable candidate verifiers for one non-secret credential prefix."""
    rows = conn.execute(
        "SELECT * FROM agent_tokens WHERE token_prefix = ? AND disabled_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?)",
        (token_prefix, now),
    ).fetchall()
    return [{**dict(row), "scopes": set(json.loads(row["scopes"]))} for row in rows]


def mark_agent_token_used(conn: sqlite3.Connection, token_id: int) -> None:
    conn.execute("UPDATE agent_tokens SET last_used_at = ? WHERE id = ?", (utc_now(), token_id))
    conn.commit()


def disable_agent_token(conn: sqlite3.Connection, *, actor: str, commit: bool = True) -> bool:
    """Revoke a credential by actor; repeated revocation remains harmless."""
    cursor = conn.execute(
        "UPDATE agent_tokens SET disabled_at = COALESCE(disabled_at, ?) WHERE actor = ?",
        (utc_now(), actor),
    )
    _maybe_commit(conn, commit)
    return cursor.rowcount > 0


def list_agent_tokens(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return safe credential metadata without raw credentials or verifiers."""
    rows = conn.execute(
        "SELECT id, token_prefix, actor, scopes, created_at, expires_at, disabled_at, last_used_at "
        "FROM agent_tokens ORDER BY created_at, id"
    ).fetchall()
    return [{**dict(row), "scopes": json.loads(row["scopes"])} for row in rows]


def insert_tracker_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    todo_id: int,
    actor: str,
    payload: dict[str, Any],
    commit: bool = True,
) -> dict[str, Any]:
    """Append an event in the caller's transaction."""
    created_at = utc_now()
    cursor = conn.execute(
        "INSERT INTO tracker_events (event_type, todo_id, actor, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, todo_id, actor, json.dumps(payload, sort_keys=True), created_at),
    )
    _maybe_commit(conn, commit)
    return {
        "id": cursor.lastrowid,
        "event_type": event_type,
        "todo_id": todo_id,
        "actor": actor,
        "payload": payload,
        "created_at": created_at,
    }


def list_tracker_events(
    conn: sqlite3.Connection, *, consumer: str, after: int, limit: int
) -> list[dict[str, Any]]:
    """Return unacknowledged events and record a delivery attempt per consumer."""
    rows = conn.execute(
        "SELECT e.* FROM tracker_events e LEFT JOIN tracker_event_deliveries d "
        "ON d.event_id = e.id AND d.consumer = ? WHERE e.id > ? AND d.acked_at IS NULL "
        "ORDER BY e.id LIMIT ?",
        (consumer, after, limit),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        conn.execute(
            "INSERT INTO tracker_event_deliveries (event_id, consumer, attempts, delivered_at) "
            "VALUES (?, ?, 1, ?) ON CONFLICT(event_id, consumer) DO UPDATE SET "
            "attempts = attempts + 1, delivered_at = excluded.delivered_at",
            (row["id"], consumer, utc_now()),
        )
        events.append({**dict(row), "payload": json.loads(row["payload_json"])})
    conn.commit()
    return events


def acknowledge_tracker_event(conn: sqlite3.Connection, *, event_id: int, consumer: str) -> bool:
    """Idempotently mark one consumer's delivery acknowledged."""
    cursor = conn.execute(
        "UPDATE tracker_event_deliveries SET acked_at = COALESCE(acked_at, ?) "
        "WHERE event_id = ? AND consumer = ?",
        (utc_now(), event_id, consumer),
    )
    conn.commit()
    return cursor.rowcount > 0
