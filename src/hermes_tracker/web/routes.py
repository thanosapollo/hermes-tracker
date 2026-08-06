"""Public read-only HTML routes."""

from __future__ import annotations

import sqlite3
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


def _cgit_urls(base: str | None, slug: str) -> dict[str, str | None]:
    if not base:
        return {"summary": None, "log": None, "tree": None}
    repo = f"{base}/{urllib.parse.quote(slug, safe='')}"
    return {"summary": repo, "log": f"{repo}/log", "tree": f"{repo}/tree"}


def _navigation(request: Request, repo: dict[str, Any], base: str | None) -> list[dict[str, str]]:
    todos = str(request.url_for("repo_detail", slug=repo["slug"]))
    configured = [item for item in repo["navigation"] if item["label"] != "todos"]
    if not configured:
        urls = _cgit_urls(base, repo["slug"])
        configured = [
            {"label": label, "url": url}
            for label in ("summary", "log", "tree")
            if (url := urls[label]) is not None
        ]
    return [*configured, {"label": "todos", "url": todos}]


def _status_color(status: str) -> str:
    return f"var(--status-{status})"


def _split(values: list[str] | None) -> list[str]:
    return [part.strip() for value in values or [] for part in value.split(",") if part.strip()]


def create_web_router(
    connection: Any, templates: Jinja2Templates, cgit_base: str | None
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    connection_dependency = Depends(connection)
    base = cgit_base
    templates.env.filters.update(
        status_color=_status_color, label_color=label_color, markdown=render_markdown
    )

    def require_repo(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
        repo = store.get_repo(conn, slug)
        if repo is None or not repo["published"]:
            raise HTTPException(status_code=404, detail="Repository not found")
        return repo

    @router.get("/", response_class=HTMLResponse, name="repo_list")
    def index(request: Request, conn: sqlite3.Connection = connection_dependency) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "repo_list.html",
            {"repos": store.list_published_repos_with_summary(conn), "cgit_base": base},
        )

    @router.get("/r/{slug}", response_class=HTMLResponse, name="repo_detail")
    def repo_detail(
        request: Request,
        slug: str,
        state: TodoListState = TodoListState.OPEN,
        q: Annotated[str | None, Query(max_length=200)] = None,
        label: Annotated[list[str] | None, Query()] = None,
        assignee: Annotated[list[str] | None, Query()] = None,
        conn: sqlite3.Connection = connection_dependency,
    ) -> Any:
        repo = require_repo(conn, slug)
        if slug != repo["slug"]:
            return RedirectResponse(
                request.url_for("repo_detail", slug=repo["slug"]), status_code=308
            )
        try:
            title_query = validate_title_query(q)
            labels = validate_filter_labels(_split(label))
            assignees = validate_filter_assignees(_split(assignee))
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
        return templates.TemplateResponse(
            request,
            "repo_detail.html",
            {
                "repo": repo,
                "todos": todos,
                "navigation": _navigation(request, repo, base),
                "has_todos": bool(todos),
                "label_suggestions": store.list_repo_labels(conn, repo["id"]),
                "assignee_suggestions": store.list_repo_assignees(conn, repo["id"]),
                "filters": {
                    "state": state.value,
                    "q": title_query or "",
                    "label": ", ".join(labels),
                    "assignee": ", ".join(assignees),
                },
            },
        )

    @router.get("/r/{slug}/{number}", response_class=HTMLResponse, name="todo_detail")
    def todo_detail(
        request: Request,
        slug: str,
        number: int,
        conn: sqlite3.Connection = connection_dependency,
    ) -> Any:
        repo = require_repo(conn, slug)
        todo = store.get_todo(conn, repo["id"], number)
        if todo is None:
            raise HTTPException(status_code=404, detail="TODO not found")
        source_repo = todo["closing_source_repo"] or repo["slug"]
        commit_url = None
        if todo["closing_commit"]:
            if base is None:
                raise HTTPException(status_code=503, detail="Closure provenance is unavailable")
            commit_url = (
                f"{base}/{urllib.parse.quote(source_repo, safe='')}/commit/"
                f"?id={urllib.parse.quote(todo['closing_commit'], safe='')}"
            )
        return templates.TemplateResponse(
            request,
            "todo_detail.html",
            {
                "repo": repo,
                "todo": todo,
                "comments": store.list_comments(conn, todo["id"]),
                "commit_url": commit_url,
                "cgit_base": base,
                "navigation": _navigation(request, repo, base),
            },
        )

    return router
