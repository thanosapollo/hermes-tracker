"""Pure domain logic: status transitions, link semantics, closure invariants.

All functions here are pure (no I/O, no side effects) so they can be unit-tested
in isolation. The store and API layers call into these helpers.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple


class TodoStatus(str, Enum):
    """Lifecycle states for a TODO."""

    OPEN = "open"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"


class TodoListState(str, Enum):
    """Public list aggregation over internal TODO statuses."""

    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


class LinkType(str, Enum):
    """Typed relationships between TODOs.

    Only BLOCKED_BY and DEPENDS_ON constrain closure behavior.
    BLOCKS and BLOCKED_BY are inverses; SUPERSEDES and SUPERSEDED_BY are inverses.
    """

    BLOCKS = "blocks"
    BLOCKED_BY = "blocked-by"
    DEPENDS_ON = "depends-on"
    DUPLICATE_OF = "duplicate-of"
    RELATED_TO = "related-to"
    SUPERSEDES = "supersedes"
    SUPERSEDED_BY = "superseded-by"


# Link types that block closure of the source TODO when the target is not done.
BLOCKING_LINK_TYPES: frozenset[LinkType] = frozenset({LinkType.BLOCKED_BY, LinkType.DEPENDS_ON})

# Inverse pairs: if (A, B, type) exists, then (B, A, inverse) is the dual view.
INVERSE_LINKS: dict[LinkType, LinkType] = {
    LinkType.BLOCKS: LinkType.BLOCKED_BY,
    LinkType.BLOCKED_BY: LinkType.BLOCKS,
    LinkType.SUPERSEDES: LinkType.SUPERSEDED_BY,
    LinkType.SUPERSEDED_BY: LinkType.SUPERSEDES,
    LinkType.DEPENDS_ON: None,  # type: ignore[dict-item]
    LinkType.DUPLICATE_OF: None,  # type: ignore[dict-item]
    LinkType.RELATED_TO: LinkType.RELATED_TO,
}

_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,49}$")
MAX_TODO_LABELS = 16
MAX_TITLE_QUERY_LENGTH = 200
MAX_FILTER_LABELS = 16
MAX_FILTER_ASSIGNEES = 16
MAX_ASSIGNEE_LENGTH = 200


class LinkError(ValueError):
    """Raised when a link type or combination is invalid."""


class ClaimError(ValueError):
    """Raised when a TODO cannot be claimed in its current state."""


class CloseError(ValueError):
    """Raised when a TODO cannot be closed."""


class LabelError(ValueError):
    """Raised when labels are invalid or oversized."""


class LinkPair(NamedTuple):
    """Directional representations of a logical link relationship."""

    forward: LinkType
    reverse: LinkType | None


def normalize_link_pair(link_type: LinkType) -> LinkPair:
    """Return both directional representations of a link.

    For symmetric/inverse pairs (blocks/blocked-by, supersedes/superseded-by,
    related-to), returns (forward, inverse). For one-directional types
    (depends-on, duplicate-of), reverse is None — only the explicit link is
    stored.
    """
    return LinkPair(forward=link_type, reverse=INVERSE_LINKS.get(link_type))


def validate_link(source_id: int, target_id: int, link_type: LinkType) -> None:
    """Validate that a link is well-formed.

    Raises:
        LinkError: if source and target are the same TODO.
    """
    if source_id == target_id:
        raise LinkError("A TODO cannot link to itself")


def can_claim(status: TodoStatus) -> bool:
    """Whether a TODO in the given status can be claimed.

    A TODO can be claimed when open or re-claimed when already active
    (reassignment). Blocked or done TODOs cannot be claimed.
    """
    return status in (TodoStatus.OPEN, TodoStatus.ACTIVE)


def claim_todo(status: TodoStatus, assignee: str | None) -> tuple[TodoStatus, str]:
    """Execute a claim transition.

    Returns the new (status, assignee) pair.

    Raises:
        ClaimError: if the status doesn't allow claiming.
    """
    if not can_claim(status):
        raise ClaimError(f"Cannot claim a TODO with status '{status.value}'")
    if not assignee or not assignee.strip():
        raise ClaimError("Claim requires an assignee")
    return TodoStatus.ACTIVE, assignee.strip()


def can_close(
    status: TodoStatus,
    blocking_links_open: bool,
    verification_output: str | None,
) -> bool:
    """Whether a TODO can be closed given its current state.

    Args:
        status: current status of the TODO.
        blocking_links_open: True if there are unresolved blocked-by/depends-on
            links (target TODOs not done).
        verification_output: the verification evidence text; must be non-empty.

    Returns:
        True if the TODO can transition to done.
    """
    if status == TodoStatus.DONE:
        return False
    if blocking_links_open:
        return False
    return bool(verification_output and verification_output.strip())


def close_todo(
    status: TodoStatus,
    blocking_links_open: bool,
    verification_output: str | None,
    closing_commit: str | None,
) -> TodoStatus:
    """Execute a close transition.

    Returns the new status (always DONE).

    Raises:
        CloseError: if closure is not allowed (already done, blocked, or no
            verification output).
    """
    if not can_close(status, blocking_links_open, verification_output):
        if status == TodoStatus.DONE:
            raise CloseError("TODO is already done")
        if blocking_links_open:
            raise CloseError("Cannot close: unresolved blocked-by or depends-on links")
        raise CloseError("Closing requires verification output")
    # closing_commit is optional; no validation needed beyond type.
    _ = closing_commit
    return TodoStatus.DONE


def normalize_labels(labels: list[str] | None) -> list[str]:
    """Normalize TODO labels: strip, lowercase, unique, sorted, bounded."""
    if not labels:
        return []
    if len(labels) > MAX_TODO_LABELS:
        raise LabelError(f"at most {MAX_TODO_LABELS} labels allowed")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in labels:
        if not isinstance(raw, str):
            raise LabelError("labels must be strings")
        label = raw.strip().lower()
        if not label:
            raise LabelError("labels cannot be empty")
        if not _LABEL_PATTERN.fullmatch(label):
            raise LabelError("label must be 1-50 chars: start alphanumeric, then [a-z0-9._-]")
        if label in seen:
            continue
        seen.add(label)
        normalized.append(label)
    if len(normalized) > MAX_TODO_LABELS:
        raise LabelError(f"at most {MAX_TODO_LABELS} labels allowed")
    return sorted(normalized)


def statuses_for_list_state(state: TodoListState) -> list[str] | None:
    """Map public list state to internal status values; None means every status."""
    if state is TodoListState.OPEN:
        return [TodoStatus.OPEN.value, TodoStatus.ACTIVE.value]
    if state is TodoListState.CLOSED:
        return [TodoStatus.DONE.value]
    return None


def validate_title_query(q: str | None) -> str | None:
    """Return stripped title query or None; reject oversized values."""
    if q is None:
        return None
    value = q.strip()
    if not value:
        return None
    if len(value) > MAX_TITLE_QUERY_LENGTH:
        raise ValueError(f"q must be at most {MAX_TITLE_QUERY_LENGTH} characters")
    return value


def validate_filter_labels(labels: list[str] | None) -> list[str]:
    """Normalize exact label filters with AND semantics and bounds."""
    if not labels:
        return []
    if len(labels) > MAX_FILTER_LABELS:
        raise LabelError(f"at most {MAX_FILTER_LABELS} label filters allowed")
    return normalize_labels(labels)


def validate_filter_assignees(assignees: list[str] | None) -> list[str]:
    """Validate exact assignee filters with OR semantics and bounds."""
    if not assignees:
        return []
    if len(assignees) > MAX_FILTER_ASSIGNEES:
        raise ValueError(f"at most {MAX_FILTER_ASSIGNEES} assignee filters allowed")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in assignees:
        value = raw.strip()
        if not value:
            raise ValueError("assignee filter cannot be empty")
        if len(value) > MAX_ASSIGNEE_LENGTH:
            raise ValueError(f"assignee must be at most {MAX_ASSIGNEE_LENGTH} characters")
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned
