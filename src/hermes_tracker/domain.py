"""Pure issue-ledger validation and filtering rules."""

from __future__ import annotations

import re
from enum import Enum


class TodoStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class TodoListState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,49}$")
MAX_TODO_LABELS = 16
MAX_ASSIGNEE_LENGTH = 200
UNKNOWN_LABEL_COLOR = "#9a8e78"
ESTABLISHED_LABEL_COLORS = {
    "bug": "#c76d52",
    "regression": "#c76d52",
    "breaking-change": "#c76d52",
    "deployment": "#c76d52",
    "feature": "#5b9bd5",
    "api": "#5b9bd5",
    "maintenance": "#8fa35b",
    "storage": "#8fa35b",
    "compatibility": "#8fa35b",
    "docs": "#9b72cf",
    "ui": "#9b72cf",
    "ux": "#9b72cf",
    "emacs": "#9b72cf",
    "research": "#4fa89b",
    "protocol": "#4fa89b",
    "build": "#d48a20",
    "packaging": "#d48a20",
    "performance": "#d48a20",
    "security": "#d45f5f",
    "needs-repro": UNKNOWN_LABEL_COLOR,
}


class LabelError(ValueError):
    pass


def normalize_assignee(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_ASSIGNEE_LENGTH:
        raise ValueError(f"assignee must be at most {MAX_ASSIGNEE_LENGTH} characters")
    return value


def normalize_labels(labels: list[str] | None) -> list[str]:
    if not labels:
        return []
    if len(labels) > MAX_TODO_LABELS:
        raise LabelError(f"at most {MAX_TODO_LABELS} labels allowed")
    normalized = {label.strip().lower() for label in labels}
    if "" in normalized:
        raise LabelError("labels cannot be empty")
    if any(not _LABEL_PATTERN.fullmatch(label) for label in normalized):
        raise LabelError("label must be 1-50 chars: start alphanumeric, then [a-z0-9._-]")
    return sorted(normalized)


def label_color(name: str) -> str:
    return ESTABLISHED_LABEL_COLORS.get(name, UNKNOWN_LABEL_COLOR)


def label_catalog() -> list[dict[str, str]]:
    return [
        {"name": name, "color": color} for name, color in sorted(ESTABLISHED_LABEL_COLORS.items())
    ]


def statuses_for_list_state(state: TodoListState) -> list[str] | None:
    return None if state is TodoListState.ALL else [state.value]


def validate_title_query(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 200:
        raise ValueError("q must be at most 200 characters")
    return value


def validate_filter_labels(values: list[str] | None) -> list[str]:
    if values and len(values) > 16:
        raise LabelError("at most 16 label filters allowed")
    return normalize_labels(values)


def validate_filter_assignees(values: list[str] | None) -> list[str]:
    if not values:
        return []
    if len(values) > 16:
        raise ValueError("at most 16 assignee filters allowed")
    cleaned = [normalize_assignee(value) for value in values if value.strip()]
    return list(dict.fromkeys(value for value in cleaned if value is not None))
