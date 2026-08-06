"""SQLite operations for repositories, issues, comments, receipts, and credentials."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .timeutil import utc_now

_TODO_SELECT = """SELECT t.*, c.commit_sha AS closing_commit,
    c.source_repo AS closing_source_repo, c.source_ref AS closing_source_ref,
    c.closed_by, c.closed_at
    FROM todos AS t LEFT JOIN todo_closures AS c ON c.todo_id=t.id"""


def _decode_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _repo(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "navigation": json.loads(row["navigation"]),
        "published": bool(row["published"]),
        "created_at": row["created_at"],
    }


def _todo(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["labels"] = _decode_list(result["labels"])
    return result


def insert_repo(
    conn: sqlite3.Connection,
    slug: str,
    name: str,
    description: str | None = None,
    navigation: list[dict[str, str]] | None = None,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO repositories(slug,name,description,navigation,created_at) VALUES(?,?,?,?,?)",
        (slug, name, description, json.dumps(navigation or []), utc_now()),
    )
    if commit:
        conn.commit()
    return get_repo(conn, slug)  # type: ignore[return-value]


def get_repo(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM repositories WHERE slug=?", (slug,)).fetchone()
    return _repo(row) if row else None


def get_repo_by_id(conn: sqlite3.Connection, repo_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
    return _repo(row) if row else None


def list_repos(conn: sqlite3.Connection, *, published_only: bool = False) -> list[dict[str, Any]]:
    where = " WHERE published=1" if published_only else ""
    rows = conn.execute(f"SELECT * FROM repositories{where} ORDER BY created_at").fetchall()  # noqa: S608
    return [_repo(row) for row in rows]


def repo_summary(conn: sqlite3.Connection, repo_id: int) -> dict[str, Any]:
    counts = {"open": 0, "closed": 0}
    for row in conn.execute(
        "SELECT status,COUNT(*) AS count FROM todos WHERE repo_id=? GROUP BY status", (repo_id,)
    ):
        counts[row["status"]] = row["count"]
    updated = conn.execute(
        "SELECT MAX(updated_at) FROM todos WHERE repo_id=?", (repo_id,)
    ).fetchone()[0]
    return {"status_counts": counts, "open_count": counts["open"], "updated_at": updated}


def list_repos_with_summary(
    conn: sqlite3.Connection, *, published_only: bool = False
) -> list[dict[str, Any]]:
    repos = list_repos(conn, published_only=published_only)
    for repo in repos:
        repo["summary"] = repo_summary(conn, repo["id"])
    return repos


def list_published_repos_with_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return list_repos_with_summary(conn, published_only=True)


def update_repo(
    conn: sqlite3.Connection, repo_id: int, fields: dict[str, Any], *, commit: bool = True
) -> dict[str, Any] | None:
    allowed = {"name", "description", "navigation", "published"}
    updates = [(key, value) for key, value in fields.items() if key in allowed]
    if updates:
        conn.execute(
            f"UPDATE repositories SET {', '.join(f'{key}=?' for key, _ in updates)} WHERE id=?",  # noqa: S608
            [json.dumps(value) if key == "navigation" else value for key, value in updates]
            + [repo_id],
        )
    if commit:
        conn.commit()
    return get_repo_by_id(conn, repo_id)


def insert_todo(
    conn: sqlite3.Connection,
    repo_id: int,
    title: str,
    description: str | None,
    priority: int,
    assignee: str | None,
    labels: list[str],
    author: str,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    number = conn.execute(
        "SELECT COALESCE(MAX(number),0)+1 FROM todos WHERE repo_id=?", (repo_id,)
    ).fetchone()[0]
    now = utc_now()
    conn.execute(
        """INSERT INTO todos(
            repo_id,number,title,description,priority,assignee,author,labels,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            repo_id,
            number,
            title,
            description,
            priority,
            assignee,
            author,
            json.dumps(labels),
            now,
            now,
        ),
    )
    if commit:
        conn.commit()
    return get_todo(conn, repo_id, number)  # type: ignore[return-value]


def get_todo(conn: sqlite3.Connection, repo_id: int, number: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"{_TODO_SELECT} WHERE t.repo_id=? AND t.number=?",  # noqa: S608
        (repo_id, number),
    ).fetchone()
    return _todo(row) if row else None


def get_todo_by_id(conn: sqlite3.Connection, todo_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"{_TODO_SELECT} WHERE t.id=?",  # noqa: S608
        (todo_id,),
    ).fetchone()
    return _todo(row) if row else None


def list_todos(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    statuses: list[str] | None = None,
    q: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> list[dict[str, Any]]:
    clauses = ["t.repo_id=?"]
    params: list[Any] = [repo_id]
    if statuses:
        clauses.append(f"t.status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if q:
        clauses.append("LOWER(t.title) LIKE ? ESCAPE '\\'")
        escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    rows = [
        _todo(row)
        for row in conn.execute(
            f"{_TODO_SELECT} WHERE {' AND '.join(clauses)} ORDER BY t.number",
            params,  # noqa: S608
        ).fetchall()
    ]
    if labels:
        required = set(labels)
        rows = [row for row in rows if required.issubset(row["labels"])]
    if assignees:
        rows = [row for row in rows if row["assignee"] in assignees]
    return rows


def list_repo_labels(conn: sqlite3.Connection, repo_id: int) -> list[str]:
    labels: set[str] = set()
    for row in conn.execute("SELECT labels FROM todos WHERE repo_id=?", (repo_id,)):
        labels.update(_decode_list(row[0]))
    return sorted(labels)


def list_repo_assignees(conn: sqlite3.Connection, repo_id: int) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT assignee FROM todos WHERE repo_id=? AND assignee IS NOT NULL "
            "AND TRIM(assignee)!='' ORDER BY assignee COLLATE NOCASE",
            (repo_id,),
        )
    ]


def update_todo(
    conn: sqlite3.Connection, todo_id: int, fields: dict[str, Any], *, commit: bool = True
) -> dict[str, Any] | None:
    allowed = {
        "title",
        "description",
        "status",
        "priority",
        "assignee",
        "labels",
    }
    current = get_todo_by_id(conn, todo_id)
    if current is None:
        return None
    updates: dict[str, Any] = {key: value for key, value in fields.items() if key in allowed}
    if updates.get("status") == "open" and current["status"] == "closed":
        conn.execute("DELETE FROM todo_closures WHERE todo_id=?", (todo_id,))
    if "labels" in updates:
        updates["labels"] = json.dumps(updates["labels"])
    if updates:
        updates["updated_at"] = utc_now()
        conn.execute(
            f"UPDATE todos SET {', '.join(f'{key}=?' for key in updates)} WHERE id=?",  # noqa: S608
            [*updates.values(), todo_id],
        )
    if commit:
        conn.commit()
    return get_todo_by_id(conn, todo_id)


def close_todo_from_git(
    conn: sqlite3.Connection,
    todo_id: int,
    *,
    commit_sha: str,
    source_repo: str,
    source_ref: str,
    actor: str,
) -> dict[str, Any] | None:
    now = utc_now()
    cursor = conn.execute(
        "UPDATE todos SET status='closed',updated_at=? WHERE id=? AND status='open'",
        (now, todo_id),
    )
    if cursor.rowcount:
        conn.execute(
            """INSERT INTO todo_closures(
                todo_id,commit_sha,source_repo,source_ref,closed_by,closed_at
            ) VALUES(?,?,?,?,?,?)""",
            (todo_id, commit_sha, source_repo, source_ref, actor, now),
        )
    return get_todo_by_id(conn, todo_id) if cursor.rowcount else None


def insert_comment(
    conn: sqlite3.Connection, todo_id: int, author: str, body: str, *, commit: bool = True
) -> dict[str, Any]:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO comments(todo_id,author,body,created_at) VALUES(?,?,?,?)",
        (todo_id, author, body, now),
    )
    if commit:
        conn.commit()
    return {"id": cursor.lastrowid, "author": author, "body": body, "created_at": now}


def list_comments(conn: sqlite3.Connection, todo_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id,author,body,created_at FROM comments WHERE todo_id=? ORDER BY id", (todo_id,)
        )
    ]


def get_mutation_receipt(
    conn: sqlite3.Connection, operation: str, key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM mutation_receipts WHERE operation=? AND idempotency_key=?",
        (operation, key),
    ).fetchone()
    return dict(row) if row else None


def insert_mutation_receipt(
    conn: sqlite3.Connection,
    *,
    operation: str,
    idempotency_key: str,
    request_hash: str,
    mutation_id: str,
    actor: str,
    status_code: int,
    response_json: str,
) -> None:
    conn.execute(
        """INSERT INTO mutation_receipts(
            operation,idempotency_key,request_hash,mutation_id,actor,status_code,response_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
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


def insert_agent_token(
    conn: sqlite3.Connection,
    *,
    actor: str,
    token_prefix: str,
    token_hash: str,
    scopes: set[str],
    expires_at: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    cursor = conn.execute(
        """INSERT INTO agent_tokens(token_prefix,actor,token_hash,scopes,created_at,expires_at)
           VALUES(?,?,?,?,?,?)""",
        (token_prefix, actor, token_hash, json.dumps(sorted(scopes)), now, expires_at),
    )
    conn.commit()
    return {"id": cursor.lastrowid, "actor": actor, "scopes": sorted(scopes), "created_at": now}


def list_active_agent_tokens(
    conn: sqlite3.Connection, prefix: str, now: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM agent_tokens WHERE token_prefix=? AND disabled_at IS NULL
           AND (expires_at IS NULL OR expires_at>?)""",
        (prefix, now),
    ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["scopes"] = json.loads(row["scopes"])
    return result


def list_agent_tokens(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = [
        dict(row)
        for row in conn.execute(
            "SELECT id,token_prefix,actor,scopes,created_at,expires_at,disabled_at "
            "FROM agent_tokens ORDER BY id"
        )
    ]
    for row in result:
        row["scopes"] = json.loads(row["scopes"])
    return result


def disable_agent_token(conn: sqlite3.Connection, *, actor: str) -> bool:
    cursor = conn.execute(
        "UPDATE agent_tokens SET disabled_at=? WHERE actor=? AND disabled_at IS NULL",
        (utc_now(), actor),
    )
    conn.commit()
    return bool(cursor.rowcount)
