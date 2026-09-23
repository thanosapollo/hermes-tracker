"""Private authenticated issue-ledger API."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import sqlite_store as store
from ..auth import principal_for_request
from ..domain import (
    UNKNOWN_LABEL_COLOR,
    LabelError,
    TodoListState,
    label_catalog,
    statuses_for_list_state,
    validate_filter_assignees,
    validate_filter_labels,
    validate_title_query,
)
from ..models import (
    CommentCreate,
    CommentResponse,
    GitClosureRequest,
    GitClosureResponse,
    LabelCatalogEntry,
    LabelCatalogResponse,
    RepoCreate,
    RepoListItem,
    RepoResponse,
    RepoTodoListResponse,
    RepoUpdate,
    TodoCreate,
    TodoResponse,
    TodoSummary,
    TodoUpdate,
)

MUTATION_HEADERS = {
    "X-Mutation-ID": {
        "description": "Stable identifier for this mutation or idempotent replay",
        "schema": {"type": "string", "format": "uuid"},
    }
}
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$"),
]
SlugPath = Annotated[str, Path(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")]
TodoNumber = Annotated[int, Path(ge=1)]


ExpectedRevision = Annotated[str | None, Header(alias="If-Match", pattern=r'^"[0-9a-f]{64}"$')]


def create_v1_router(connection: Any, check_auth: Any, cgit_base: str | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(check_auth)])
    connection_dependency = Depends(connection)

    def transaction(conn: sqlite3.Connection, action: Callable[[], Any]) -> Any:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    def require_repo(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
        repo = store.get_repo(conn, slug)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"Repository '{slug}' not found")
        return repo

    def require_todo(conn: sqlite3.Connection, repo_id: int, number: int) -> dict[str, Any]:
        todo = store.get_todo(conn, repo_id, number)
        if todo is None:
            raise HTTPException(status_code=404, detail=f"TODO #{number} not found in repository")
        return todo

    def todo_response(conn: sqlite3.Connection, todo: dict[str, Any]) -> TodoResponse:
        repo = store.get_repo_by_id(conn, todo["repo_id"])
        payload = {
            **{key: value for key, value in todo.items() if key != "repo_id"},
            "repo_slug": repo["slug"] if repo else "",
            "comments": store.list_comments(conn, todo["id"]),
        }
        revision = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return TodoResponse(**payload, revision=revision)

    def mutate(
        conn: sqlite3.Connection,
        *,
        operation: str,
        body: BaseModel,
        response: Response,
        action: Callable[[], BaseModel],
        status_code: int,
        key: str,
        actor: str,
        expected_revision: str | None = None,
    ) -> BaseModel | JSONResponse:
        # Preserve historical fingerprints for clients without a precondition.
        fingerprint = {"actor": actor, "request": body.model_dump(mode="json")}
        if expected_revision is not None:
            fingerprint["if_match"] = expected_revision
            # Omitted fields and explicit null have different PATCH semantics.
            fingerprint["fields_set"] = sorted(body.model_fields_set)
        request_hash = hashlib.sha256(
            json.dumps(
                fingerprint,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

        def locked() -> BaseModel | JSONResponse:
            receipt = store.get_mutation_receipt(conn, operation, key)
            if receipt:
                if receipt["actor"] != actor or receipt["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used with different input",
                    )
                payload = json.loads(receipt["response_json"])
                headers = {"X-Mutation-ID": receipt["mutation_id"]}
                if "revision" in payload:
                    headers["ETag"] = f'"{payload["revision"]}"'
                return JSONResponse(
                    content=payload, status_code=receipt["status_code"], headers=headers
                )
            mutation_id = str(uuid4())
            result = action()
            payload = result.model_dump(mode="json")
            store.insert_mutation_receipt(
                conn,
                operation=operation,
                idempotency_key=key,
                request_hash=request_hash,
                mutation_id=mutation_id,
                actor=actor,
                status_code=status_code,
                response_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )
            response.headers["X-Mutation-ID"] = mutation_id
            if isinstance(result, TodoResponse):
                response.headers["ETag"] = f'"{result.revision}"'
            return result

        return transaction(conn, locked)

    @router.post("/repos", response_model=RepoResponse, status_code=201)
    def create_repo(
        req: RepoCreate, conn: sqlite3.Connection = connection_dependency
    ) -> RepoResponse:
        def action() -> RepoResponse:
            if store.get_repo(conn, req.slug):
                raise HTTPException(
                    status_code=409, detail=f"Repository '{req.slug}' already exists"
                )
            return RepoResponse(
                **store.insert_repo(
                    conn,
                    req.slug,
                    req.name,
                    req.description,
                    [item.model_dump() for item in req.navigation],
                    commit=False,
                )
            )

        return transaction(conn, action)

    @router.get("/repos", response_model=list[RepoListItem])
    def list_repos(conn: sqlite3.Connection = connection_dependency) -> list[RepoListItem]:
        return [RepoListItem(**repo) for repo in store.list_repos_with_summary(conn)]

    @router.patch("/repos/{slug}", response_model=RepoResponse)
    def update_repo(
        slug: SlugPath, req: RepoUpdate, conn: sqlite3.Connection = connection_dependency
    ) -> RepoResponse:
        def action() -> RepoResponse:
            repo = require_repo(conn, slug)
            updated = store.update_repo(
                conn, repo["id"], req.model_dump(exclude_unset=True), commit=False
            )
            return RepoResponse(**updated)  # type: ignore[arg-type]

        return transaction(conn, action)

    @router.post(
        "/repos/{slug}/todos",
        response_model=TodoResponse,
        status_code=201,
        responses={201: {"headers": MUTATION_HEADERS}},
    )
    def create_todo(
        slug: SlugPath,
        req: TodoCreate,
        response: Response,
        request: Request,
        idempotency_key: IdempotencyKey,
        conn: sqlite3.Connection = connection_dependency,
    ) -> TodoResponse | JSONResponse:
        actor = principal_for_request(request).actor

        def action() -> TodoResponse:
            repo = require_repo(conn, slug)
            return todo_response(
                conn,
                store.insert_todo(
                    conn,
                    repo["id"],
                    req.title,
                    req.description,
                    req.priority,
                    req.assignee,
                    req.labels,
                    actor,
                    commit=False,
                ),
            )

        return mutate(
            conn,
            operation=f"create-todo:{slug}",
            body=req,
            response=response,
            action=action,
            status_code=201,
            key=idempotency_key,
            actor=actor,
        )  # type: ignore[return-value]

    @router.get("/repos/{slug}/todos", response_model=RepoTodoListResponse)
    def list_todos(
        slug: SlugPath,
        request: Request,
        conn: sqlite3.Connection = connection_dependency,
        state: TodoListState = TodoListState.OPEN,
        q: Annotated[str | None, Query(max_length=200)] = None,
        label: Annotated[list[str] | None, Query()] = None,
        assignee: Annotated[list[str] | None, Query()] = None,
    ) -> RepoTodoListResponse:
        if set(request.query_params) - {"state", "q", "label", "assignee"}:
            raise HTTPException(status_code=422, detail="unknown query parameter")
        repo = require_repo(conn, slug)
        try:
            todos = store.list_todos(
                conn,
                repo["id"],
                statuses=statuses_for_list_state(state),
                q=validate_title_query(q),
                labels=validate_filter_labels(label),
                assignees=validate_filter_assignees(assignee),
            )
        except (LabelError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RepoTodoListResponse(
            repo=RepoResponse(**repo),
            todos=[
                TodoSummary(
                    number=todo["number"],
                    title=todo["title"],
                    status=todo["status"],
                    priority=todo["priority"],
                    assignee=todo["assignee"],
                    author=todo["author"],
                    labels=todo["labels"],
                    updated_at=todo["updated_at"],
                    closed_at=todo["closed_at"],
                )
                for todo in todos
            ],
        )

    @router.get("/repos/{slug}/todos/{number}", response_model=TodoResponse)
    def get_todo(
        slug: SlugPath,
        number: TodoNumber,
        response: Response,
        conn: sqlite3.Connection = connection_dependency,
    ) -> TodoResponse:
        # One read snapshot for issue fields, closure provenance and discussion.
        conn.execute("BEGIN")
        try:
            repo = require_repo(conn, slug)
            result = todo_response(conn, require_todo(conn, repo["id"], number))
        finally:
            conn.rollback()
        response.headers["ETag"] = f'"{result.revision}"'
        return result

    @router.patch(
        "/repos/{slug}/todos/{number}",
        response_model=TodoResponse,
        responses={200: {"headers": MUTATION_HEADERS}},
    )
    def update_todo(
        slug: SlugPath,
        number: TodoNumber,
        req: TodoUpdate,
        response: Response,
        request: Request,
        idempotency_key: IdempotencyKey,
        conn: sqlite3.Connection = connection_dependency,
        expected_revision: ExpectedRevision = None,
    ) -> TodoResponse | JSONResponse:
        actor = principal_for_request(request).actor

        def action() -> TodoResponse:
            repo = require_repo(conn, slug)
            todo = require_todo(conn, repo["id"], number)
            if expected_revision is not None:
                current = todo_response(conn, todo)
                if expected_revision != f'"{current.revision}"':
                    raise HTTPException(
                        status_code=412,
                        detail="Stale revision; read the TODO again before editing",
                    )
            fields = req.model_dump(exclude_unset=True, mode="json")
            if todo["status"] == "closed" and fields != {"status": "open"}:
                raise HTTPException(
                    status_code=409,
                    detail="Closed TODOs only accept reopen via status=open",
                )
            updated = store.update_todo(conn, todo["id"], fields, commit=False)
            return todo_response(conn, updated)  # type: ignore[arg-type]

        return mutate(
            conn,
            operation=f"update-todo:{slug}:{number}",
            expected_revision=expected_revision,
            body=req,
            response=response,
            action=action,
            status_code=200,
            key=idempotency_key,
            actor=actor,
        )  # type: ignore[return-value]

    @router.post(
        "/repos/{slug}/todos/{number}/comments",
        response_model=CommentResponse,
        status_code=201,
        responses={201: {"headers": MUTATION_HEADERS}},
    )
    def create_comment(
        slug: SlugPath,
        number: TodoNumber,
        req: CommentCreate,
        response: Response,
        request: Request,
        idempotency_key: IdempotencyKey,
        conn: sqlite3.Connection = connection_dependency,
    ) -> CommentResponse | JSONResponse:
        actor = principal_for_request(request).actor

        def action() -> CommentResponse:
            repo = require_repo(conn, slug)
            todo = require_todo(conn, repo["id"], number)
            return CommentResponse(
                **store.insert_comment(conn, todo["id"], actor, req.body, commit=False)
            )

        return mutate(
            conn,
            operation=f"create-comment:{slug}:{number}",
            body=req,
            response=response,
            action=action,
            status_code=201,
            key=idempotency_key,
            actor=actor,
        )  # type: ignore[return-value]

    @router.get("/repos/{slug}/todos/{number}/comments", response_model=list[CommentResponse])
    def list_comments(
        slug: SlugPath, number: TodoNumber, conn: sqlite3.Connection = connection_dependency
    ) -> list[CommentResponse]:
        repo = require_repo(conn, slug)
        todo = require_todo(conn, repo["id"], number)
        return [CommentResponse(**comment) for comment in store.list_comments(conn, todo["id"])]

    @router.get("/labels", response_model=LabelCatalogResponse)
    def get_labels() -> LabelCatalogResponse:
        return LabelCatalogResponse(
            labels=[LabelCatalogEntry(**item) for item in label_catalog()],
            fallback_color=UNKNOWN_LABEL_COLOR,
        )

    @router.post(
        "/git-closures",
        response_model=GitClosureResponse,
        responses={200: {"headers": MUTATION_HEADERS}},
    )
    def close_from_git(
        req: GitClosureRequest,
        response: Response,
        request: Request,
        idempotency_key: IdempotencyKey,
        conn: sqlite3.Connection = connection_dependency,
    ) -> GitClosureResponse | JSONResponse:
        if cgit_base is None:
            raise HTTPException(
                status_code=503, detail="Git closure requires a valid HTTPS cgit base"
            )
        actor = principal_for_request(request).actor

        def action() -> GitClosureResponse:
            repo = store.get_repo(conn, req.target_repo)
            todo = store.get_todo(conn, repo["id"], req.target_number) if repo else None
            outcome: Literal["closed", "already_closed", "missing"]
            if todo is None:
                outcome = "missing"
            elif todo["status"] == "closed":
                outcome = "already_closed"
            else:
                closed = store.close_todo_from_git(
                    conn,
                    todo["id"],
                    commit_sha=req.commit_sha,
                    source_repo=req.source_repo,
                    source_ref=req.source_ref,
                    actor=actor,
                )
                outcome = "already_closed" if closed is None else "closed"
            return GitClosureResponse(
                outcome=outcome,
                target_repo=req.target_repo,
                target_number=req.target_number,
            )

        return mutate(
            conn,
            operation="git-closure",
            body=req,
            response=response,
            action=action,
            status_code=200,
            key=idempotency_key,
            actor=actor,
        )  # type: ignore[return-value]

    return router
