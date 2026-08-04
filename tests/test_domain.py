"""Tests for pure domain logic: status transitions, link semantics, closure."""

from __future__ import annotations

import pytest

from hermes_tracker.domain import (
    BLOCKING_LINK_TYPES,
    ESTABLISHED_LABEL_COLORS,
    INVERSE_LINKS,
    UNKNOWN_LABEL_COLOR,
    ClaimError,
    CloseError,
    LinkError,
    LinkPair,
    LinkType,
    ReleaseError,
    TodoStatus,
    can_claim,
    can_close,
    can_release,
    claim_todo,
    close_todo,
    label_catalog,
    label_color,
    normalize_link_pair,
    release_todo,
    validate_link,
)


class TestStatusEnum:
    def test_four_statuses(self) -> None:
        assert TodoStatus.OPEN.value == "open"
        assert TodoStatus.ACTIVE.value == "active"
        assert TodoStatus.BLOCKED.value == "blocked"
        assert TodoStatus.DONE.value == "done"

    def test_status_count(self) -> None:
        assert len(list(TodoStatus)) == 4


class TestLinkTypeEnum:
    def test_seven_link_types(self) -> None:
        assert len(list(LinkType)) == 7

    @pytest.mark.parametrize(
        "lt",
        [
            LinkType.BLOCKS,
            LinkType.BLOCKED_BY,
            LinkType.DEPENDS_ON,
            LinkType.DUPLICATE_OF,
            LinkType.RELATED_TO,
            LinkType.SUPERSEDES,
            LinkType.SUPERSEDED_BY,
        ],
    )
    def test_link_type_values(self, lt: LinkType) -> None:
        assert lt.value  # non-empty string


class TestBlockingLinkTypes:
    def test_only_blocked_by_and_depends_on_block_closure(self) -> None:
        assert LinkType.BLOCKED_BY in BLOCKING_LINK_TYPES
        assert LinkType.DEPENDS_ON in BLOCKING_LINK_TYPES

    def test_non_blocking_types(self) -> None:
        assert LinkType.BLOCKS not in BLOCKING_LINK_TYPES
        assert LinkType.RELATED_TO not in BLOCKING_LINK_TYPES
        assert LinkType.DUPLICATE_OF not in BLOCKING_LINK_TYPES
        assert LinkType.SUPERSEDES not in BLOCKING_LINK_TYPES
        assert LinkType.SUPERSEDED_BY not in BLOCKING_LINK_TYPES


class TestInverseLinks:
    def test_blocks_inverse(self) -> None:
        assert INVERSE_LINKS[LinkType.BLOCKS] == LinkType.BLOCKED_BY

    def test_blocked_by_inverse(self) -> None:
        assert INVERSE_LINKS[LinkType.BLOCKED_BY] == LinkType.BLOCKS

    def test_supersedes_inverse(self) -> None:
        assert INVERSE_LINKS[LinkType.SUPERSEDES] == LinkType.SUPERSEDED_BY

    def test_superseded_by_inverse(self) -> None:
        assert INVERSE_LINKS[LinkType.SUPERSEDED_BY] == LinkType.SUPERSEDES

    def test_related_to_is_symmetric(self) -> None:
        assert INVERSE_LINKS[LinkType.RELATED_TO] == LinkType.RELATED_TO

    def test_depends_on_no_inverse(self) -> None:
        assert INVERSE_LINKS[LinkType.DEPENDS_ON] is None

    def test_duplicate_of_no_inverse(self) -> None:
        assert INVERSE_LINKS[LinkType.DUPLICATE_OF] is None


class TestNormalizeLinkPair:
    def test_blocks_pair(self) -> None:
        pair = normalize_link_pair(LinkType.BLOCKS)
        assert pair == LinkPair(forward=LinkType.BLOCKS, reverse=LinkType.BLOCKED_BY)

    def test_depends_on_pair(self) -> None:
        pair = normalize_link_pair(LinkType.DEPENDS_ON)
        assert pair == LinkPair(forward=LinkType.DEPENDS_ON, reverse=None)


class TestValidateLink:
    def test_self_link_rejected(self) -> None:
        with pytest.raises(LinkError, match="cannot link to itself"):
            validate_link(1, 1, LinkType.BLOCKS)

    def test_valid_link_passes(self) -> None:
        validate_link(1, 2, LinkType.BLOCKS)  # should not raise


class TestClaim:
    def test_claim_open_todo(self) -> None:
        status, assignee = claim_todo(TodoStatus.OPEN, "agent-1")
        assert status == TodoStatus.ACTIVE
        assert assignee == "agent-1"

    def test_claim_active_todo_reassign(self) -> None:
        status, assignee = claim_todo(TodoStatus.ACTIVE, "agent-2")
        assert status == TodoStatus.ACTIVE
        assert assignee == "agent-2"

    def test_claim_blocked_todo_rejected(self) -> None:
        with pytest.raises(ClaimError):
            claim_todo(TodoStatus.BLOCKED, "agent-1")

    def test_claim_done_todo_rejected(self) -> None:
        with pytest.raises(ClaimError):
            claim_todo(TodoStatus.DONE, "agent-1")

    def test_claim_without_assignee_rejected(self) -> None:
        with pytest.raises(ClaimError):
            claim_todo(TodoStatus.OPEN, None)  # type: ignore[arg-type]

    def test_claim_empty_assignee_rejected(self) -> None:
        with pytest.raises(ClaimError):
            claim_todo(TodoStatus.OPEN, "  ")

    def test_can_claim_truth_table(self) -> None:
        assert can_claim(TodoStatus.OPEN) is True
        assert can_claim(TodoStatus.ACTIVE) is True
        assert can_claim(TodoStatus.BLOCKED) is False
        assert can_claim(TodoStatus.DONE) is False


class TestRelease:
    def test_release_active_todo(self) -> None:
        assert release_todo(TodoStatus.ACTIVE, "worker", "worker") == (TodoStatus.OPEN, None)

    def test_release_open_todo_is_idempotent(self) -> None:
        assert release_todo(TodoStatus.OPEN, None, "worker") == (TodoStatus.OPEN, None)

    def test_release_active_todo_requires_current_assignee(self) -> None:
        with pytest.raises(ReleaseError, match="current assignee"):
            release_todo(TodoStatus.ACTIVE, "worker", "intruder")

    def test_release_open_assigned_todo_is_rejected(self) -> None:
        with pytest.raises(ReleaseError):
            release_todo(TodoStatus.OPEN, "worker", "worker")

    @pytest.mark.parametrize("status", [TodoStatus.BLOCKED, TodoStatus.DONE])
    def test_release_invalid_status_rejected(self, status: TodoStatus) -> None:
        with pytest.raises(ReleaseError):
            release_todo(status, None, "worker")

    def test_can_release_truth_table(self) -> None:
        assert can_release(TodoStatus.OPEN, None, "worker") is True
        assert can_release(TodoStatus.OPEN, "worker", "worker") is False
        assert can_release(TodoStatus.ACTIVE, "worker", "worker") is True
        assert can_release(TodoStatus.ACTIVE, "worker", "intruder") is False
        assert can_release(TodoStatus.BLOCKED, None, "worker") is False
        assert can_release(TodoStatus.DONE, None, "worker") is False


class TestClose:
    def test_close_open_with_verification(self) -> None:
        result = close_todo(
            TodoStatus.OPEN,
            blocking_links_open=False,
            verification_output="all tests pass",
            closing_commit=None,
        )
        assert result == TodoStatus.DONE

    def test_close_with_commit_sha(self) -> None:
        result = close_todo(
            TodoStatus.ACTIVE,
            blocking_links_open=False,
            verification_output="ok",
            closing_commit="abc123",
        )
        assert result == TodoStatus.DONE

    def test_close_without_verification_rejected(self) -> None:
        with pytest.raises(CloseError, match="verification"):
            close_todo(
                TodoStatus.OPEN,
                blocking_links_open=False,
                verification_output=None,
                closing_commit=None,
            )

    def test_close_empty_verification_rejected(self) -> None:
        with pytest.raises(CloseError, match="verification"):
            close_todo(
                TodoStatus.OPEN,
                blocking_links_open=False,
                verification_output="  ",
                closing_commit=None,
            )

    def test_close_blocked_by_dependency_rejected(self) -> None:
        with pytest.raises(CloseError, match="blocked-by or depends-on"):
            close_todo(
                TodoStatus.OPEN,
                blocking_links_open=True,
                verification_output="ok",
                closing_commit=None,
            )

    def test_close_already_done_rejected(self) -> None:
        with pytest.raises(CloseError, match="already done"):
            close_todo(
                TodoStatus.DONE,
                blocking_links_open=False,
                verification_output="ok",
                closing_commit=None,
            )

    def test_can_close_truth_table(self) -> None:
        # (status, blocking_open, verification) -> expected
        assert can_close(TodoStatus.OPEN, False, "ok") is True
        assert can_close(TodoStatus.ACTIVE, False, "ok") is True
        assert can_close(TodoStatus.OPEN, True, "ok") is False
        assert can_close(TodoStatus.OPEN, False, None) is False
        assert can_close(TodoStatus.DONE, False, "ok") is False


class TestLabelPalette:
    EXPECTED = {
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
        "needs-repro": "#9a8e78",
    }

    def test_unknown_fallback_color(self) -> None:
        assert UNKNOWN_LABEL_COLOR == "#9a8e78"

    def test_established_colors_exact(self) -> None:
        assert ESTABLISHED_LABEL_COLORS == self.EXPECTED

    def test_label_color_known_and_unknown(self) -> None:
        assert label_color("bug") == "#c76d52"
        assert label_color("security") == "#d45f5f"
        assert label_color("needs-repro") == "#9a8e78"
        assert label_color("auth") == UNKNOWN_LABEL_COLOR
        assert label_color("totally-custom") == UNKNOWN_LABEL_COLOR

    def test_label_catalog_sorted_pairs(self) -> None:
        catalog = label_catalog()
        assert [entry["name"] for entry in catalog] == sorted(self.EXPECTED)
        assert catalog == [
            {"name": name, "color": color} for name, color in sorted(self.EXPECTED.items())
        ]
