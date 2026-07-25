"""Application configuration via environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

# The old Agentum filename requires an explicit operator-owned data migration.
DEFAULT_DB_PATH = "hermes_tracker.db"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables.

    Attributes:
        db_path: SQLite database file path.
        auth_token: Bearer token required for API mutations. If None, auth is
            disabled (useful for tests and local development).
        cgit_base: Canonical cgit base URL for upstream repository links
            (e.g. ``https://git.example.org``). When set and a matching
            repo slug exists in cgit, the UI links ``summary``/``log``/``tree``
            to the exact canonical URL. When unset or empty, the links are
            omitted entirely rather than guessed.
    """

    db_path: str
    auth_token: str | None
    root_path: str = ""
    cgit_base: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=_env("HERMES_TRACKER_DB", DEFAULT_DB_PATH) or DEFAULT_DB_PATH,
            auth_token=_env("HERMES_TRACKER_AUTH_TOKEN"),
            root_path=_env("HERMES_TRACKER_ROOT_PATH", "") or "",
            cgit_base=_env("HERMES_TRACKER_CGIT_BASE", "") or "",
        )
