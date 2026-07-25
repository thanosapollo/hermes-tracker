"""Pydantic request/response models for the API v1 schema."""

from __future__ import annotations

import re
import urllib.parse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .public_content import validate_public_comment

# --- Request models ---

_NAVIGATION_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
_KANBAN_TASK_ID = re.compile(r"t_[0-9a-f]{8}")


class NavigationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    url: str = Field(..., min_length=1, max_length=2000)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not _NAVIGATION_LABEL.fullmatch(value):
            raise ValueError("label must be a lowercase navigation identifier")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("URL cannot contain control characters")
        try:
            parsed = urllib.parse.urlsplit(value)
            _ = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid navigation URL") from exc
        if value.startswith("/") and not value.startswith("//"):
            if parsed.query or parsed.fragment:
                raise ValueError("relative navigation URL cannot contain query or fragment")
            return value
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("navigation URL must be absolute HTTPS or root-relative")
        return value


class NavigationMixin(BaseModel):
    @model_validator(mode="after")
    def reject_duplicate_navigation_labels(self) -> NavigationMixin:
        navigation = getattr(self, "navigation", None)
        if navigation is not None:
            labels = [item.label for item in navigation]
            if len(labels) != len(set(labels)):
                raise ValueError("navigation labels must be unique")
        return self


class RepoCreate(NavigationMixin):
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    navigation: list[NavigationItem] = Field(default_factory=list, max_length=50)


class RepoUpdate(NavigationMixin):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    navigation: list[NavigationItem] | None = Field(default=None, max_length=50)
    published: bool = True

    @field_validator("name")
    @classmethod
    def reject_null_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be null")
        return value


class TodoCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)
    assignee: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)


class TodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    assignee: str | None = None
    acceptance_criteria: list[str] | None = None
    verification_commands: list[str] | None = None


class ClaimRequest(BaseModel):
    assignee: str = Field(..., min_length=1, max_length=200)


class CloseRequest(BaseModel):
    verification_output: str = Field(..., min_length=1)
    closing_commit: str | None = Field(default=None, max_length=40)


class CommentCreate(BaseModel):
    author: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1)

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        return validate_public_comment(value)


class LinkCreate(BaseModel):
    target_repo_slug: str
    target_number: int = Field(..., ge=1)
    link_type: str  # validated against domain.LinkType in the API layer


class ExternalReferenceCreate(BaseModel):
    system: str = Field(..., min_length=1, max_length=100)
    external_id: str = Field(..., min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, str] = Field(default_factory=dict)


class KanbanBoardIdentityBackfill(BaseModel):
    board: str = Field(..., min_length=1, max_length=200)


class SupersedeKanbanExecutionRequest(BaseModel):
    rationale: str = Field(..., min_length=1)
    replacement_kanban_id: str | None = None

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        return validate_public_comment(value)

    @field_validator("replacement_kanban_id")
    @classmethod
    def validate_replacement_kanban_id(cls, value: str | None) -> str | None:
        if value is not None and not _KANBAN_TASK_ID.fullmatch(value):
            raise ValueError("replacement_kanban_id must match t_<8 lowercase hex characters>")
        return value


# --- Response models ---


class RepoResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str | None
    navigation: list[NavigationItem] = Field(default_factory=list)
    published: bool
    created_at: str


class CommentResponse(BaseModel):
    id: int
    author: str
    body: str
    created_at: str


class LinkResponse(BaseModel):
    id: int
    link_type: str
    target_repo_slug: str
    target_number: int
    target_title: str


class ExternalReferenceResponse(BaseModel):
    id: int
    system: str
    external_id: str
    url: str | None
    metadata: dict[str, str]
    created_at: str


class SupersededKanbanExecutionResponse(BaseModel):
    task_id: str
    rationale: str
    replacement_kanban_id: str | None
    created_at: str


class TodoResponse(BaseModel):
    id: int
    number: int
    repo_slug: str
    title: str
    description: str | None
    status: str
    priority: int
    assignee: str | None
    author: str | None
    acceptance_criteria: list[str]
    verification_commands: list[str]
    closing_commit: str | None
    verification_output: str | None
    closed_by: str | None
    closed_at: str | None
    created_at: str
    updated_at: str
    comments: list[CommentResponse]
    links: list[LinkResponse]
    external_references: list[ExternalReferenceResponse] = Field(default_factory=list)
    superseded_kanban_executions: list[SupersededKanbanExecutionResponse] = Field(
        default_factory=list
    )


class TodoSummary(BaseModel):
    number: int
    title: str
    status: str
    priority: int
    assignee: str | None
    author: str | None
    updated_at: str


class RepoTodoListResponse(BaseModel):
    repo: RepoResponse
    todos: list[TodoSummary]
