"""Injectable UTC timestamp provider for testability."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()
