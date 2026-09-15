"""Strict API request and response models."""

from __future__ import annotations

import re
import urllib.parse
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain import LabelError, TodoStatus, normalize_assignee, normalize_labels
from .public_content import validate_public_comment, validate_public_text

# Request-only: existing records must remain readable without a content migration.
PublicText = Annotated[str, AfterValidator(validate_public_text)]

_NAVIGATION_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
_REPO_SLUG = re.compile(r"^[a-zA-Z0-9_.-]+$")


def _valid_repo_slug(value: str) -> str:
    if not _REPO_SLUG.fullmatch(value) or value in {".", ".."}:
        raise ValueError("repository slug must be a stable cgit path segment")
    return validate_public_text(value)


def _valid_git_ref(value: str) -> str:
    forbidden = " ~^:?*[\\"
    parts = value.split("/")
    if (
        not value.startswith("refs/")
        or len(parts) < 2
        or any(not part or part.startswith(".") or part.endswith(".lock") for part in parts)
        or value.endswith(".")
        or ".." in value
        or "@{" in value
        or any(ord(char) < 32 or ord(char) == 127 or char in forbidden for char in value)
    ):
        raise ValueError("source_ref must be a valid full Git ref")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NavigationItem(StrictModel):
    label: str
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("label")
    @classmethod
    def valid_label(cls, value: str) -> str:
        if not _NAVIGATION_LABEL.fullmatch(value):
            raise ValueError("label must be a lowercase navigation identifier")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
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


class NavigationMixin(StrictModel):
    @field_validator("navigation", check_fields=False)
    @classmethod
    def public_navigation(cls, value: list[NavigationItem] | None) -> list[NavigationItem] | None:
        if value is not None:
            for item in value:
                validate_public_text(item.label)
                validate_public_text(item.url)
        return value

    @model_validator(mode="after")
    def unique_navigation(self) -> NavigationMixin:
        navigation = getattr(self, "navigation", None)
        if navigation is not None:
            labels = [item.label for item in navigation]
            if len(labels) != len(set(labels)):
                raise ValueError("navigation labels must be unique")
        return self


class RepoCreate(NavigationMixin):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    name: PublicText = Field(min_length=1, max_length=200)
    description: PublicText | None = Field(default=None, max_length=20_000)
    navigation: list[NavigationItem] = Field(default_factory=list, max_length=50)

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        return _valid_repo_slug(value)


class RepoUpdate(NavigationMixin):
    name: PublicText | None = Field(default=None, min_length=1, max_length=200)
    description: PublicText | None = Field(default=None, max_length=20_000)
    navigation: list[NavigationItem] | None = Field(default=None, max_length=50)
    published: bool | None = None

    @field_validator("name", "navigation", "published")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @model_validator(mode="after")
    def require_field(self) -> RepoUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class TodoCreate(StrictModel):
    title: PublicText = Field(min_length=1, max_length=500)
    description: PublicText | None = Field(default=None, max_length=20_000)
    priority: int = Field(default=0, ge=-1000, le=1000)
    assignee: PublicText | None = Field(default=None, max_length=200)
    labels: list[PublicText] = Field(default_factory=list, max_length=16)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title cannot be blank")
        return value

    @field_validator("assignee")
    @classmethod
    def valid_assignee(cls, value: str | None) -> str | None:
        return normalize_assignee(value)

    @field_validator("labels")
    @classmethod
    def valid_labels(cls, value: list[str]) -> list[str]:
        try:
            return normalize_labels(value)
        except LabelError as exc:
            raise ValueError(str(exc)) from exc


class TodoUpdate(StrictModel):
    title: PublicText | None = Field(default=None, min_length=1, max_length=500)
    description: PublicText | None = Field(default=None, max_length=20_000)
    status: Literal["open"] | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    assignee: PublicText | None = Field(default=None, max_length=200)
    labels: list[PublicText] | None = Field(default=None, max_length=16)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("field cannot be null")
        value = value.strip()
        if not value:
            raise ValueError("title cannot be blank")
        return value

    @field_validator("status", "priority", "labels")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("field cannot be null")
        return value

    @field_validator("assignee")
    @classmethod
    def valid_assignee(cls, value: str | None) -> str | None:
        return normalize_assignee(value)

    @field_validator("labels")
    @classmethod
    def valid_labels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        try:
            return normalize_labels(value)
        except LabelError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def require_field(self) -> TodoUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class GitClosureRequest(StrictModel):
    source_repo: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    source_ref: PublicText = Field(min_length=6, max_length=500)
    commit_sha: str = Field(pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
    target_repo: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    target_number: int = Field(ge=1)

    @field_validator("commit_sha")
    @classmethod
    def lower_sha(cls, value: str) -> str:
        return value.lower()

    @field_validator("source_repo", "target_repo")
    @classmethod
    def valid_repo_slug(cls, value: str) -> str:
        return _valid_repo_slug(value)

    @field_validator("source_ref")
    @classmethod
    def valid_source_ref(cls, value: str) -> str:
        return _valid_git_ref(value)


class GitClosureResponse(StrictModel):
    outcome: Literal["closed", "already_closed", "missing"]
    target_repo: str
    target_number: int


class CommentCreate(StrictModel):
    body: str = Field(min_length=1, max_length=20_000)

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: str) -> str:
        return validate_public_comment(value)


class RepoResponse(StrictModel):
    id: int
    slug: str
    name: str
    description: str | None
    navigation: list[NavigationItem]
    published: bool
    created_at: str


class RepoSummary(StrictModel):
    status_counts: dict[str, int]
    open_count: int
    updated_at: str | None


class RepoListItem(RepoResponse):
    summary: RepoSummary


class CommentResponse(StrictModel):
    id: int
    author: str
    body: str
    created_at: str


class TodoResponse(StrictModel):
    id: int
    number: int
    repo_slug: str
    title: str
    description: str | None
    status: TodoStatus
    priority: int
    assignee: str | None
    author: str | None
    labels: list[str]
    verification_output: str | None
    closing_commit: str | None
    closing_source_repo: str | None
    closing_source_ref: str | None
    closed_by: str | None
    closed_at: str | None
    created_at: str
    updated_at: str
    comments: list[CommentResponse]


class TodoSummary(StrictModel):
    number: int
    title: str
    status: TodoStatus
    priority: int
    assignee: str | None
    author: str | None
    labels: list[str]
    updated_at: str
    closed_at: str | None


class RepoTodoListResponse(StrictModel):
    repo: RepoResponse
    todos: list[TodoSummary]


class LabelCatalogEntry(StrictModel):
    name: str
    color: str


class LabelCatalogResponse(StrictModel):
    labels: list[LabelCatalogEntry]
    fallback_color: str
