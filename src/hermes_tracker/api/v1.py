"""FastAPI API v1 router: JSON endpoints for agent mutations and reads."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from threading import Lock
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import sqlite_store as store
from ..auth import principal_for_request
from ..domain import (
    UNKNOWN_LABEL_COLOR,
    ClaimError,
    CloseError,
    LabelError,
    LinkError,
    LinkType,
    TodoListState,
    TodoStatus,
    claim_todo,
    close_todo,
    label_catalog,
    statuses_for_list_state,
    validate_filter_assignees,
    validate_filter_labels,
    validate_link,
    validate_title_query,
)
from ..models import (
    ClaimRequest,
    CloseRequest,
    CommentCreate,
    CommentResponse,
    ExternalReferenceCreate,
    ExternalReferenceResponse,
    KanbanBoardIdentityBackfill,
    LabelCatalogEntry,
    LabelCatalogResponse,
    LinkCreate,
    LinkResponse,
    RepoCreate,
    RepoResponse,
    RepoTodoListResponse,
    RepoUpdate,
    SupersededKanbanExecutionResponse,
    SupersedeKanbanExecutionRequest,
    TodoCreate,
    TodoResponse,
    TodoSummary,
    TodoUpdate,
)

MUTATION_ID_HEADER = {
    "X-Mutation-ID": {
        "description": "Stable identifier for this mutation or idempotent replay",
        "schema": {"type": "string", "format": "uuid"},
    }
}
_KANBAN_TASK_ID = re.compile(r"t_[0-9a-f]{8}")


def create_v1_router(get_conn: Any, check_auth: Any) -> APIRouter:
    """Create the v1 API router.

    Args:
        get_conn: callable returning a sqlite3.Connection.
        check_auth: FastAPI dependency function for auth validation.
    """
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(check_auth)])

    def _conn() -> sqlite3.Connection:
        return get_conn()

    write_lock = Lock()

    def _write(action: Callable[[sqlite3.Connection], Any]) -> Any:
        """Serialize one complete write transaction on the shared connection."""
        with write_lock:
            conn = _conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = action(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _mutate(
        operation: str | Callable[[sqlite3.Connection], str],
        request: BaseModel,
        response: Response,
        action: Callable[[sqlite3.Connection], BaseModel],
        status_code: int,
        idempotency_key: str | None,
        actor: str | None,
    ) -> BaseModel | JSONResponse:
        return _write(
            lambda conn: _mutate_locked(
                conn, operation, request, response, action, status_code, idempotency_key, actor
            )
        )

    def _mutate_locked(
        conn: sqlite3.Connection,
        operation: str | Callable[[sqlite3.Connection], str],
        request: BaseModel,
        response: Response,
        action: Callable[[sqlite3.Connection], BaseModel],
        status_code: int,
        idempotency_key: str | None,
        actor: str | None,
    ) -> BaseModel | JSONResponse:
        operation_name = operation(conn) if callable(operation) else operation
        request_json = request.model_dump_json(exclude_none=False)
        request_hash = hashlib.sha256(
            json.dumps(
                {"actor": actor, "request": json.loads(request_json)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if idempotency_key:
            receipt = store.get_mutation_receipt(conn, operation_name, idempotency_key)
            if receipt:
                if receipt["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key was already used with different input",
                    )
                return JSONResponse(
                    content=json.loads(receipt["response_json"]),
                    status_code=receipt["status_code"],
                    headers={"X-Mutation-ID": receipt["mutation_id"]},
                )
        mutation_id = str(uuid4())
        result = action(conn)
        payload = result.model_dump(mode="json")
        if idempotency_key:
            store.insert_mutation_receipt(
                conn,
                operation=operation_name,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                mutation_id=mutation_id,
                actor=actor,
                status_code=status_code,
                response_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )
        response.headers["X-Mutation-ID"] = mutation_id
        return result

    # ----- Durable asynchronous event outbox -----

    @router.get("/events")
    def list_events(
        consumer: str = Query(min_length=1, max_length=200),
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "events": store.list_tracker_events(
                _conn(), consumer=consumer, after=after, limit=limit
            )
        }

    @router.post("/events/{event_id}/ack")
    def acknowledge_event(
        event_id: int,
        consumer: str = Query(min_length=1, max_length=200),
    ) -> dict[str, bool]:
        return {
            "acknowledged": store.acknowledge_tracker_event(
                _conn(), event_id=event_id, consumer=consumer
            )
        }

    # ----- Repositories -----

    @router.post("/repos", response_model=RepoResponse, status_code=201)
    def create_repo(req: RepoCreate) -> RepoResponse:
        def action(conn: sqlite3.Connection) -> RepoResponse:
            if store.get_repo(conn, req.slug):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Repository '{req.slug}' already exists",
                )
            repo = store.insert_repo(
                conn,
                req.slug,
                req.name,
                req.description,
                [item.model_dump() for item in req.navigation],
                commit=False,
            )
            return RepoResponse(**repo)

        return _write(action)

    @router.get("/repos", response_model=list[RepoResponse])
    def list_repos() -> list[RepoResponse]:
        return [RepoResponse(**r) for r in store.list_repos(_conn())]

    @router.patch("/repos/{slug}", response_model=RepoResponse)
    def update_repo(slug: str, req: RepoUpdate) -> RepoResponse:
        def action(conn: sqlite3.Connection) -> RepoResponse:
            repo = store.get_repo(conn, slug)
            if not repo:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Repository '{slug}' not found",
                )
            updated = store.update_repo(
                conn, repo["id"], req.model_dump(exclude_unset=True), commit=False
            )
            return RepoResponse(**updated)  # type: ignore[arg-type]

        return _write(action)

    # ----- Helpers -----

    def _require_repo(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
        repo = store.get_repo(conn, slug)
        if not repo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{slug}' not found",
            )
        return repo

    def _require_todo(conn: sqlite3.Connection, repo_id: int, number: int) -> dict[str, Any]:
        todo = store.get_todo(conn, repo_id, number)
        if not todo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"TODO #{number} not found in repository",
            )
        return todo

    def _todo_operation(prefix: str, slug: str, number: int) -> Callable[[sqlite3.Connection], str]:
        def resolve(conn: sqlite3.Connection) -> str:
            repo = _require_repo(conn, slug)
            todo = _require_todo(conn, repo["id"], number)
            return f"{prefix}:{todo['id']}"

        return resolve

    def _to_todo_response(conn: sqlite3.Connection, todo: dict[str, Any]) -> TodoResponse:
        comments = [
            CommentResponse(
                id=c["id"], author=c["author"], body=c["body"], created_at=c["created_at"]
            )
            for c in store.list_comments(conn, todo["id"])
        ]
        links = [
            LinkResponse(
                id=lnk["id"],
                link_type=lnk["link_type"],
                target_repo_slug=lnk["target_repo_slug"],
                target_number=lnk["target_number"],
                target_title=lnk["target_title"],
            )
            for lnk in store.list_links(conn, todo["id"])
        ]
        return TodoResponse(
            id=todo["id"],
            number=todo["number"],
            repo_slug=_repo_slug_by_id(conn, todo["repo_id"]),
            title=todo["title"],
            description=todo["description"],
            status=todo["status"],
            priority=todo["priority"],
            assignee=todo["assignee"],
            author=todo["author"],
            acceptance_criteria=todo["acceptance_criteria"],
            verification_commands=todo["verification_commands"],
            labels=todo.get("labels") or [],
            closing_commit=todo["closing_commit"],
            verification_output=todo["verification_output"],
            closed_by=todo["closed_by"],
            closed_at=todo["closed_at"],
            created_at=todo["created_at"],
            updated_at=todo["updated_at"],
            comments=comments,
            links=links,
            external_references=[
                ExternalReferenceResponse(**reference)
                for reference in store.list_external_references(conn, todo["id"])
            ],
            superseded_kanban_executions=[
                SupersededKanbanExecutionResponse(**execution)
                for execution in store.list_superseded_kanban_executions(conn, todo["id"])
            ],
        )

    def _repo_slug_by_id(conn: sqlite3.Connection, repo_id: int) -> str:
        repo = store.get_repo_by_id(conn, repo_id)
        return repo["slug"] if repo else ""

    def _mutation_actor(request: Request, supplied_actor: str | None) -> str:
        """Use legacy attribution only for the trusted master credential."""
        principal = principal_for_request(request)
        return supplied_actor if principal.is_master and supplied_actor else principal.actor

    # ----- Todos -----

    @router.post(
        "/repos/{slug}/todos",
        response_model=TodoResponse,
        status_code=201,
        responses={201: {"headers": MUTATION_ID_HEADER}},
    )
    def create_todo(
        slug: str,
        req: TodoCreate,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: str | None = Header(default=None, alias="X-Actor"),
    ) -> TodoResponse | JSONResponse:
        conn = _conn()
        repo = _require_repo(conn, slug)
        mutation_actor = _mutation_actor(request, actor)

        def action(conn: sqlite3.Connection) -> TodoResponse:
            todo = store.insert_todo(
                conn,
                repo["id"],
                title=req.title,
                description=req.description,
                priority=req.priority,
                assignee=req.assignee,
                acceptance_criteria=req.acceptance_criteria,
                verification_commands=req.verification_commands,
                labels=req.labels,
                author=mutation_actor,
                commit=False,
            )
            if todo["assignee"]:
                store.insert_tracker_event(
                    conn,
                    event_type="todo.assigned",
                    todo_id=todo["id"],
                    actor=mutation_actor,
                    payload={
                        "repo_slug": slug,
                        "number": todo["number"],
                        "assignee": todo["assignee"],
                    },
                    commit=False,
                )
            return _to_todo_response(conn, todo)

        return _mutate(
            f"create-todo:{repo['id']}", req, response, action, 201, idempotency_key, mutation_actor
        )  # type: ignore[return-value]

    @router.get("/repos/{slug}/todos", response_model=RepoTodoListResponse)
    def list_todos(
        slug: str,
        state: TodoListState = TodoListState.OPEN,
        status_filter: TodoStatus | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        label: Annotated[list[str] | None, Query()] = None,
        assignee: Annotated[list[str] | None, Query()] = None,
    ) -> RepoTodoListResponse:
        conn = _conn()
        repo = _require_repo(conn, slug)
        try:
            title_query = validate_title_query(q)
            labels = validate_filter_labels(label or [])
            assignees = validate_filter_assignees(assignee or [])
        except (LabelError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        if status_filter is not None:
            todos = store.list_todos(
                conn,
                repo["id"],
                status=status_filter.value,
                q=title_query,
                labels=labels,
                assignees=assignees,
            )
        else:
            todos = store.list_todos(
                conn,
                repo["id"],
                statuses=statuses_for_list_state(state),
                q=title_query,
                labels=labels,
                assignees=assignees,
            )
        return RepoTodoListResponse(
            repo=RepoResponse(**repo),
            todos=[
                TodoSummary(
                    number=t["number"],
                    title=t["title"],
                    status=t["status"],
                    priority=t["priority"],
                    assignee=t["assignee"],
                    author=t["author"],
                    labels=t.get("labels") or [],
                    updated_at=t["updated_at"],
                )
                for t in todos
            ],
        )

    @router.get("/repos/{slug}/todos/{number}", response_model=TodoResponse)
    def get_todo(slug: str, number: int) -> TodoResponse:
        conn = _conn()
        repo = _require_repo(conn, slug)
        todo = _require_todo(conn, repo["id"], number)
        return _to_todo_response(conn, todo)

    @router.patch("/repos/{slug}/todos/{number}", response_model=TodoResponse)
    def update_todo(slug: str, number: int, req: TodoUpdate) -> TodoResponse:
        def action(conn: sqlite3.Connection) -> TodoResponse:
            repo = _require_repo(conn, slug)
            todo = _require_todo(conn, repo["id"], number)
            updated = store.update_todo(
                conn, todo["id"], req.model_dump(exclude_unset=True), commit=False
            )
            return _to_todo_response(conn, updated)  # type: ignore[arg-type]

        return _write(action)

    @router.post(
        "/repos/{slug}/todos/{number}/claim",
        response_model=TodoResponse,
        responses={200: {"headers": MUTATION_ID_HEADER}},
    )
    def claim_todo_endpoint(
        slug: str,
        number: int,
        req: ClaimRequest,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: str | None = Header(default=None, alias="X-Actor"),
    ) -> TodoResponse | JSONResponse:
        mutation_actor = _mutation_actor(request, actor)

        def action(conn: sqlite3.Connection) -> TodoResponse:
            repo = _require_repo(conn, slug)
            todo = _require_todo(conn, repo["id"], number)
            try:
                new_status, assignee = claim_todo(TodoStatus(todo["status"]), req.assignee)
            except ClaimError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            store.set_todo_status(conn, todo["id"], new_status, commit=False)
            store.set_todo_assignee(conn, todo["id"], assignee, commit=False)
            updated = store.get_todo_by_id(conn, todo["id"])
            store.insert_tracker_event(
                conn,
                event_type="todo.claimed",
                todo_id=todo["id"],
                actor=mutation_actor,
                payload={"repo_slug": slug, "number": todo["number"], "assignee": assignee},
                commit=False,
            )
            return _to_todo_response(conn, updated)  # type: ignore[arg-type]

        return _mutate(
            _todo_operation("claim-todo", slug, number),
            req,
            response,
            action,
            200,
            idempotency_key,
            mutation_actor,
        )  # type: ignore[return-value]

    @router.post(
        "/repos/{slug}/todos/{number}/close",
        response_model=TodoResponse,
        responses={200: {"headers": MUTATION_ID_HEADER}},
    )
    def close_todo_endpoint(
        slug: str,
        number: int,
        req: CloseRequest,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: str | None = Header(default=None, alias="X-Actor"),
    ) -> TodoResponse | JSONResponse:
        mutation_actor = _mutation_actor(request, actor)

        def action(conn: sqlite3.Connection) -> TodoResponse:
            repo = _require_repo(conn, slug)
            todo = _require_todo(conn, repo["id"], number)
            try:
                close_todo(
                    status=TodoStatus(todo["status"]),
                    blocking_links_open=store.has_open_blocking_links(conn, todo["id"]),
                    verification_output=req.verification_output,
                    closing_commit=req.closing_commit,
                )
            except CloseError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            updated = store.close_todo(
                conn,
                todo["id"],
                verification_output=req.verification_output,
                closing_commit=req.closing_commit,
                closed_by=actor if principal_for_request(request).is_master else mutation_actor,
                commit=False,
            )
            store.insert_tracker_event(
                conn,
                event_type="todo.closed",
                todo_id=todo["id"],
                actor=mutation_actor,
                payload={"repo_slug": slug, "number": todo["number"]},
                commit=False,
            )
            return _to_todo_response(conn, updated)  # type: ignore[arg-type]

        return _mutate(
            _todo_operation("close-todo", slug, number),
            req,
            response,
            action,
            200,
            idempotency_key,
            mutation_actor,
        )  # type: ignore[return-value]

    # ----- Comments -----

    @router.post(
        "/repos/{slug}/todos/{number}/comments",
        response_model=CommentResponse,
        status_code=201,
        responses={201: {"headers": MUTATION_ID_HEADER}},
    )
    def create_comment(
        slug: str,
        number: int,
        req: CommentCreate,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: str | None = Header(default=None, alias="X-Actor"),
    ) -> CommentResponse | JSONResponse:
        conn = _conn()
        repo = _require_repo(conn, slug)
        todo = _require_todo(conn, repo["id"], number)
        comment_author = _mutation_actor(request, req.author)

        def action(conn: sqlite3.Connection) -> CommentResponse:
            comment = store.insert_comment(conn, todo["id"], comment_author, req.body, commit=False)
            if "@tracker" in req.body.lower():
                store.insert_tracker_event(
                    conn,
                    event_type="todo.comment.requested",
                    todo_id=todo["id"],
                    actor=comment_author,
                    payload={
                        "repo_slug": slug,
                        "number": todo["number"],
                        "comment_id": comment["id"],
                    },
                    commit=False,
                )
            return CommentResponse(
                id=comment["id"],
                author=comment["author"],
                body=comment["body"],
                created_at=comment["created_at"],
            )

        return _mutate(
            f"create-comment:{todo['id']}",
            req,
            response,
            action,
            201,
            idempotency_key,
            comment_author,
        )  # type: ignore[return-value]

    @router.get(
        "/repos/{slug}/todos/{number}/comments",
        response_model=list[CommentResponse],
    )
    def list_comments_endpoint(slug: str, number: int) -> list[CommentResponse]:
        conn = _conn()
        repo = _require_repo(conn, slug)
        todo = _require_todo(conn, repo["id"], number)
        comments = store.list_comments(conn, todo["id"])
        return [
            CommentResponse(
                id=c["id"], author=c["author"], body=c["body"], created_at=c["created_at"]
            )
            for c in comments
        ]

    # ----- Links -----

    @router.post(
        "/repos/{slug}/todos/{number}/links",
        response_model=LinkResponse,
        status_code=201,
        responses={201: {"headers": MUTATION_ID_HEADER}},
    )
    def create_link(
        slug: str,
        number: int,
        req: LinkCreate,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: str | None = Header(default=None, alias="X-Actor"),
    ) -> LinkResponse | JSONResponse:
        conn = _conn()
        repo = _require_repo(conn, slug)
        source_todo = _require_todo(conn, repo["id"], number)
        mutation_actor = _mutation_actor(request, actor)
        # Resolve target todo
        target_repo = store.get_repo(conn, req.target_repo_slug)
        if not target_repo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Target repository '{req.target_repo_slug}' not found",
            )
        target_todo = store.get_todo(conn, target_repo["id"], req.target_number)
        if not target_todo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Target TODO #{req.target_number} not found",
            )
        # Validate link type
        try:
            link_type = LinkType(req.link_type)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid link type '{req.link_type}'",
            ) from None
        try:
            validate_link(source_todo["id"], target_todo["id"], link_type)
        except LinkError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

        def action(conn: sqlite3.Connection) -> LinkResponse:
            try:
                link = store.insert_link(
                    conn, source_todo["id"], target_todo["id"], link_type, commit=False
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Link already exists or violates constraint",
                ) from exc
            return LinkResponse(
                id=link["id"],
                link_type=link["link_type"],
                target_repo_slug=target_repo["slug"],
                target_number=target_todo["number"],
                target_title=target_todo["title"],
            )

        return _mutate(
            f"create-link:{source_todo['id']}",
            req,
            response,
            action,
            201,
            idempotency_key,
            mutation_actor,
        )  # type: ignore[return-value]

    @router.get(
        "/repos/{slug}/todos/{number}/links",
        response_model=list[LinkResponse],
    )
    def list_links_endpoint(slug: str, number: int) -> list[LinkResponse]:
        conn = _conn()
        repo = _require_repo(conn, slug)
        todo = _require_todo(conn, repo["id"], number)
        links = store.list_links(conn, todo["id"])
        return [
            LinkResponse(
                id=lnk["id"],
                link_type=lnk["link_type"],
                target_repo_slug=lnk["target_repo_slug"],
                target_number=lnk["target_number"],
                target_title=lnk["target_title"],
            )
            for lnk in links
        ]

    @router.post(
        "/repos/{slug}/todos/{number}/kanban-executions/{task_id}/supersede",
        response_model=SupersededKanbanExecutionResponse,
        status_code=201,
        responses={201: {"headers": MUTATION_ID_HEADER}},
    )
    def supersede_kanban_execution_endpoint(
        slug: str,
        number: int,
        task_id: str,
        req: SupersedeKanbanExecutionRequest,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        actor: str | None = Header(default=None, alias="X-Actor"),
    ) -> SupersededKanbanExecutionResponse | JSONResponse:
        if not _KANBAN_TASK_ID.fullmatch(task_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="task_id must match t_<8 lowercase hex characters>",
            )
        if req.replacement_kanban_id == task_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="replacement_kanban_id must differ from task_id",
            )
        conn = _conn()
        repo = _require_repo(conn, slug)
        todo = _require_todo(conn, repo["id"], number)
        mutation_actor = _mutation_actor(request, actor)
        linked = store.list_external_references(conn, todo["id"])
        is_linked = any(
            ref["system"] == "hermes-kanban" and ref["external_id"] == task_id for ref in linked
        )
        if not is_linked:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Kanban card is not linked"
            )

        def action(conn: sqlite3.Connection) -> SupersededKanbanExecutionResponse:
            try:
                execution = store.supersede_kanban_execution(
                    conn, todo["id"], task_id, req.rationale, req.replacement_kanban_id
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Kanban execution is already superseded",
                ) from exc
            return SupersededKanbanExecutionResponse(**execution)

        return _mutate(
            f"supersede-kanban-execution:{todo['id']}:{task_id}",
            req,
            response,
            action,
            201,
            idempotency_key,
            mutation_actor,
        )  # type: ignore[return-value]

    # ----- External references -----

    @router.post(
        "/repos/{slug}/todos/{number}/external-references",
        response_model=ExternalReferenceResponse,
        status_code=201,
    )
    def create_external_reference(
        slug: str, number: int, req: ExternalReferenceCreate
    ) -> ExternalReferenceResponse:
        def action(conn: sqlite3.Connection) -> ExternalReferenceResponse:
            repo = _require_repo(conn, slug)
            todo = _require_todo(conn, repo["id"], number)
            try:
                reference = store.insert_external_reference(
                    conn,
                    todo["id"],
                    req.system,
                    req.external_id,
                    req.url,
                    req.metadata,
                    commit=False,
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="External reference already belongs to a TODO",
                ) from exc
            return ExternalReferenceResponse(**reference)

        return _write(action)

    @router.patch(
        "/repos/{slug}/todos/{number}/external-references/{reference_id}/kanban-board-identity",
        response_model=ExternalReferenceResponse,
    )
    def backfill_kanban_board_identity(
        slug: str, number: int, reference_id: int, req: KanbanBoardIdentityBackfill
    ) -> ExternalReferenceResponse:
        def action(conn: sqlite3.Connection) -> ExternalReferenceResponse:
            repo = _require_repo(conn, slug)
            todo = _require_todo(conn, repo["id"], number)
            reference = store.get_external_reference(conn, todo["id"], reference_id)
            if reference is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="External reference not found"
                )
            if reference["system"] != "hermes-kanban" or not _KANBAN_TASK_ID.fullmatch(
                reference["external_id"]
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="External reference is not a canonical Hermes Kanban task",
                )
            existing_board = reference["metadata"].get("board")
            if existing_board is not None:
                if existing_board != req.board:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Kanban board identity already differs",
                    )
                return ExternalReferenceResponse(**reference)
            metadata = {**reference["metadata"], "board": req.board}
            store.backfill_external_reference_board_identity(conn, reference_id, metadata)
            return ExternalReferenceResponse(**{**reference, "metadata": metadata})

        return _write(action)

    @router.get(
        "/repos/{slug}/todos/{number}/external-references",
        response_model=list[ExternalReferenceResponse],
    )
    def list_external_references(slug: str, number: int) -> list[ExternalReferenceResponse]:
        conn = _conn()
        repo = _require_repo(conn, slug)
        todo = _require_todo(conn, repo["id"], number)
        return [
            ExternalReferenceResponse(**reference)
            for reference in store.list_external_references(conn, todo["id"])
        ]

    @router.get("/labels", response_model=LabelCatalogResponse)
    def get_label_catalog() -> LabelCatalogResponse:
        """Return the global established-label palette and unknown-label fallback."""
        return LabelCatalogResponse(
            labels=[LabelCatalogEntry(**entry) for entry in label_catalog()],
            fallback_color=UNKNOWN_LABEL_COLOR,
        )

    return router
