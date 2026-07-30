"""Server-rendered public read-only HTML routes."""

from __future__ import annotations

import sqlite3
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import sqlite_store as store
from ..domain import (
    LabelError,
    TodoListState,
    label_color,
    statuses_for_list_state,
    validate_filter_assignees,
    validate_filter_labels,
    validate_title_query,
)
from .markdown import render_markdown


def _safe_cgit_base(raw: str) -> str | None:
    """Return a validated https cgit base URL without trailing slash, or None."""
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    if path:
        base += path
    return base


def _cgit_urls(base: str | None, slug: str) -> dict[str, str | None]:
    """Derive canonical cgit navigation URLs for a repository slug.

    Returns a dict with ``summary``/``log``/``tree`` keys. When *base* is
    falsy (no cgit configured), all values are None — the template omits the
    links entirely rather than fabricating URLs.
    """
    if not base:
        return {"summary": None, "log": None, "tree": None, "base": None}
    quoted = urllib.parse.quote(slug, safe="")
    repo_base = f"{base}/{quoted}"
    return {
        "summary": repo_base,
        "log": f"{repo_base}/log",
        "tree": f"{repo_base}/tree",
        "base": base,
    }


def _cgit_commit_url(base: str | None, slug: str, commit: str | None) -> str | None:
    if not base or not commit:
        return None
    repo = urllib.parse.quote(slug, safe="")
    revision = urllib.parse.quote(commit, safe="")
    return f"{base}/{repo}/commit/?id={revision}"


def _repository_navigation(
    request: Request,
    repo: dict[str, Any],
    cgit_base: str | None,
) -> list[dict[str, str]]:
    """Return repository navigation with one tracker-owned todos destination."""
    todos_url = str(request.url_for("repo_detail", slug=repo["slug"]))
    navigation = repo.get("navigation") or []
    if not navigation:
        cgit_urls = _cgit_urls(cgit_base, repo["slug"])
        fallback = [
            {"label": label, "url": url}
            for label in ("summary", "log", "tree")
            if (url := cgit_urls[label]) is not None
        ]
        return [*fallback, {"label": "todos", "url": todos_url}]

    rendered = []
    has_todos = False
    for item in navigation:
        if item["label"] != "todos":
            rendered.append({"label": item["label"], "url": item["url"]})
        elif not has_todos:
            rendered.append({"label": "todos", "url": todos_url})
            has_todos = True
    if not has_todos:
        rendered.append({"label": "todos", "url": todos_url})
    return rendered


def _status_color(status: str) -> str:
    """Return the semantic CSS token for a tracker status."""
    return {
        "open": "var(--status-open)",
        "active": "var(--status-active)",
        "blocked": "var(--status-blocked)",
        "done": "var(--status-done)",
    }.get(status, "var(--status-other)")


def _label_color(label: str) -> str:
    """Return palette hex for a label; never echoes untrusted style input."""
    return label_color(label)


def _split_filter_values(raw: list[str] | None) -> list[str]:
    """Expand repeated and comma-separated public filter values."""
    values: list[str] = []
    for item in raw or []:
        for part in item.split(","):
            part = part.strip()
            if part:
                values.append(part)
    return values


def create_web_router(
    get_conn: Any,
    templates: Jinja2Templates,
    cgit_base: str = "",
) -> APIRouter:
    """Create the public read-only HTML router.

    Args:
        get_conn: callable returning a sqlite3.Connection.
        templates: configured Jinja2Templates instance.
        cgit_base: canonical cgit base URL. Validated at startup; if it does
            not resolve to an allowed https origin, cgit navigation links are
            omitted from the UI entirely.
    """
    router = APIRouter()

    templates.env.filters["status_color"] = _status_color
    templates.env.filters["label_color"] = _label_color
    templates.env.filters["markdown"] = render_markdown

    validated_base = _safe_cgit_base(cgit_base)

    def _conn() -> sqlite3.Connection:
        return get_conn()

    def _require_repo(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
        repo = store.get_repo(conn, slug)
        if not repo or not repo["published"]:
            raise HTTPException(status_code=404, detail="Repository not found")
        return repo

    def _is_published_repo(conn: sqlite3.Connection, slug: str) -> bool:
        repo = store.get_repo(conn, slug)
        return bool(repo and repo["published"])

    @router.get("/", response_class=HTMLResponse, name="repo_list")
    def index(request: Request) -> HTMLResponse:
        repos = store.list_published_repos_with_summary(_conn())
        return templates.TemplateResponse(
            request,
            "repo_list.html",
            {"repos": repos, "cgit_base": validated_base},
        )

    @router.get("/r/{slug}", response_class=HTMLResponse, name="repo_detail")
    def repo_detail(
        request: Request,
        slug: str,
        state: TodoListState = TodoListState.OPEN,
        q: Annotated[str | None, Query(max_length=200)] = None,
        label: Annotated[list[str] | None, Query()] = None,
        assignee: Annotated[list[str] | None, Query()] = None,
    ) -> Any:
        conn = _conn()
        repo = _require_repo(conn, slug)
        if slug != repo["slug"]:
            target = str(request.url_for("repo_detail", slug=repo["slug"]))
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(target, status_code=308)
        try:
            title_query = validate_title_query(q)
            labels = validate_filter_labels(_split_filter_values(label or []))
            assignees = validate_filter_assignees(_split_filter_values(assignee or []))
        except (LabelError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        todos = store.list_todos(
            conn,
            repo["id"],
            statuses=statuses_for_list_state(state),
            q=title_query,
            labels=labels,
            assignees=assignees,
        )
        label_suggestions = store.list_repo_labels(conn, repo["id"])
        assignee_suggestions = store.list_repo_assignees(conn, repo["id"])
        return templates.TemplateResponse(
            request,
            "repo_detail.html",
            {
                "repo": repo,
                "todos": todos,
                "navigation": _repository_navigation(request, repo, validated_base),
                "has_todos": len(todos) > 0,
                "label_suggestions": label_suggestions,
                "assignee_suggestions": assignee_suggestions,
                "filters": {
                    "state": state.value,
                    "q": title_query or "",
                    "label": ", ".join(labels),
                    "assignee": ", ".join(assignees),
                },
            },
        )

    @router.get("/r/{slug}/{number}", response_class=HTMLResponse, name="todo_detail")
    def todo_detail(request: Request, slug: str, number: int) -> Any:
        conn = _conn()
        repo = _require_repo(conn, slug)
        if slug != repo["slug"]:
            return RedirectResponse(
                request.url_for("todo_detail", slug=repo["slug"], number=number),
                status_code=308,
            )
        todo = store.get_todo(conn, repo["id"], number)
        if not todo:
            raise HTTPException(status_code=404, detail="TODO not found")
        comments = store.list_comments(conn, todo["id"])
        links = [
            link
            for link in store.list_links(conn, todo["id"])
            if _is_published_repo(conn, link["target_repo_slug"])
        ]
        reverse_links = [
            link
            for link in store.list_reverse_links(conn, todo["id"])
            if _is_published_repo(conn, link["source_repo_slug"])
        ]
        return templates.TemplateResponse(
            request,
            "todo_detail.html",
            {
                "repo": repo,
                "todo": todo,
                "comments": comments,
                "links": links,
                "reverse_links": reverse_links,
                "commit_url": _cgit_commit_url(
                    validated_base, repo["slug"], todo["closing_commit"]
                ),
                "cgit_base": validated_base,
                "navigation": _repository_navigation(request, repo, validated_base),
            },
        )

    return router
