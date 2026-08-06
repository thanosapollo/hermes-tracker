"""Application configuration via environment variables."""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from dataclasses import dataclass

# The old Agentum filename requires an explicit operator-owned data migration.
DEFAULT_DB_PATH = "hermes_tracker.db"
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_URI_PATH = re.compile(r"^(?:/[A-Za-z0-9._~!$&'()*+,;=:@%-]*)*$")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value else default


def normalize_cgit_base(value: str) -> str | None:
    """Return a canonical HTTPS cgit base, or None for unsafe input."""
    if (
        not value
        or _BAD_PERCENT.search(value)
        or any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value)
    ):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or _BAD_PERCENT.search(parsed.path)
        or not _URI_PATH.fullmatch(parsed.path)
    ):
        return None
    hostname = parsed.hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if (
            not hostname.isascii()
            or len(hostname) > 253
            or all(char in "0123456789." for char in hostname)
            or any(not _DNS_LABEL.fullmatch(label) for label in hostname.split("."))
        ):
            return None
        canonical_host = hostname.lower()
    else:
        canonical_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    decoded_path = urllib.parse.unquote(parsed.path)
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in decoded_path) or any(
        part in {".", ".."} for part in decoded_path.split("/")
    ):
        return None
    netloc = canonical_host + (f":{port}" if port is not None else "")
    return urllib.parse.urlunsplit(("https", netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables.

    Attributes:
        db_path: SQLite database file path.
        auth_token: Optional administrative bearer token. Scoped credentials
            remain valid when it is absent; unknown credentials never do.
        cgit_base: Canonical cgit base URL for upstream repository links
            (e.g. ``https://git.example.org``). When set and a matching
            repo slug exists in cgit, the UI links ``summary``/``log``/``tree``
            to the exact canonical URL. Git closure fails closed when this is
            absent or invalid.
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
