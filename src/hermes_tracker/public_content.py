"""Validation for text persisted to publicly readable tracker history."""

from __future__ import annotations

import re

_PROHIBITED_PATTERNS = (
    (
        "credential",
        re.compile(
            r"(?i)(?:authorization\s*:\s*bearer\s+\S+|"
            r"(?:api[_-]?key|token|password|secret)\s*[=:]\s*\S+)"
        ),
    ),
    (
        "private filesystem path",
        re.compile(r"(?i)(?:/(?:home|root|etc|var|run|srv)/\S+|~/(?:\.?[^\s]+))"),
    ),
    (
        "raw log or traceback",
        re.compile(
            r"(?im)(?:traceback \(most recent call last\):|"
            r"^\s*File \"[^\"]+\", line \d+|"
            r"^\d{4}-\d{2}-\d{2}[T ][^\n]*(?:ERROR|DEBUG|WARNING))"
        ),
    ),
    (
        "hidden infrastructure",
        re.compile(
            r"(?i)(?:https?://(?:10(?:\.\d{1,3}){3}|127(?:\.\d{1,3}){3}|"
            r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
            r"(?::\d+)?|\b[a-z0-9-]+\.(?:internal|local|lan)(?::\d+)?)"
        ),
    ),
)


def validate_public_comment(body: str) -> str:
    """Return public-safe text or raise ``ValueError`` with a categorized reason."""
    if not body.strip():
        raise ValueError("Public tracker comment cannot be empty")
    for category, pattern in _PROHIBITED_PATTERNS:
        if pattern.search(body):
            raise ValueError(f"Public tracker comment contains prohibited {category} material")
    return body
