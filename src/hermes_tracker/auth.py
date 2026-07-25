"""Injectable authentication and scope authorization for Tracker's private API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status

from . import sqlite_store as store
from .config import Settings

ALL_SCOPES = frozenset(
    {
        "read",
        "repo:manage",
        "todo:create",
        "todo:update",
        "todo:claim",
        "todo:close",
        "comment:create",
        "todo:link",
    }
)


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated server-owned identity and authorization grants."""

    actor: str
    scopes: frozenset[str]
    is_master: bool = False


def generate_token() -> str:
    """Return an opaque credential suitable for one-time operator display."""
    return f"htk_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """Return a salted scrypt verifier; callers must never persist ``token``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1)
    return ":".join(
        (
            "scrypt-v1",
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        )
    )


def verify_token_hash(token: str, encoded_hash: str) -> bool:
    """Verify an opaque token without exposing parser errors to API callers."""
    try:
        algorithm, encoded_salt, encoded_digest = encoded_hash.split(":", 2)
        if algorithm != "scrypt-v1":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode())
        expected = base64.urlsafe_b64decode(encoded_digest.encode())
        actual = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1)
    except (ValueError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


def token_prefix(token: str) -> str:
    """Return a non-secret lookup prefix stored beside a credential verifier."""
    return token[:12]


def make_auth_dependency(
    settings: Settings, get_conn: Callable[[], sqlite3.Connection]
) -> Callable[[Request], None]:
    """Create an API dependency that authenticates and authorizes every request."""

    def check_auth(request: Request) -> None:
        if settings.auth_token is None:
            principal = Principal("local-development", ALL_SCOPES, is_master=True)
        else:
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                _unauthorized("Missing or malformed Authorization header")
            provided = header[len("Bearer ") :]
            if hmac.compare_digest(provided, settings.auth_token):
                principal = Principal("master", ALL_SCOPES, is_master=True)
            else:
                agent_principal = _find_agent_principal(get_conn(), provided)
                if agent_principal is None:
                    _unauthorized("Invalid authorization token")
                    raise AssertionError("unreachable")
                principal = agent_principal
        request.state.tracker_principal = principal
        require_scope(request, _scope_for_request(request))

    return check_auth


def principal_for_request(request: Request) -> Principal:
    """Read the principal installed by the router-wide auth dependency."""
    principal = getattr(request.state, "tracker_principal", None)
    if not isinstance(principal, Principal):
        raise RuntimeError("Tracker principal was not initialized")
    return principal


def require_scope(request: Request, scope: str) -> None:
    """Reject a valid credential that lacks a route's least-privilege scope."""
    if scope not in principal_for_request(request).scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Credential lacks required scope"
        )


def _find_agent_principal(conn: sqlite3.Connection, token: str) -> Principal | None:
    for record in store.list_active_agent_tokens(conn, token_prefix(token), _utc_now()):
        if verify_token_hash(token, record["token_hash"]):
            store.mark_agent_token_used(conn, record["id"])
            return Principal(record["actor"], frozenset(record["scopes"]))
    return None


def _scope_for_request(request: Request) -> str:
    path = request.url.path.rstrip("/")
    method = request.method
    if path.startswith("/api/v1/events") or method == "GET":
        return "read"
    if path == "/api/v1/repos":
        return "repo:manage"
    if method == "PATCH" and path.startswith("/api/v1/repos/") and path.count("/") == 4:
        return "repo:manage"
    if (
        path.endswith("/external-references")
        or path.endswith("/links")
        or path.endswith("/kanban-board-identity")
    ):
        return "todo:link"
    if path.endswith("/claim"):
        return "todo:claim"
    if path.endswith("/close") or path.endswith("/supersede"):
        return "todo:close"
    if path.endswith("/comments"):
        return "comment:create"
    if path.endswith("/todos"):
        return "todo:create"
    return "todo:update"


def _unauthorized(detail: str) -> None:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
