"""Bearer authentication and least-privilege route scopes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import sqlite_store as store
from .config import Settings

CLIENT_SCOPES = frozenset({"read", "repo:manage", "todo:create", "todo:update", "comment:create"})
ALL_SCOPES = CLIENT_SCOPES | {"git:close"}
BEARER = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Security(BEARER)]


@dataclass(frozen=True, slots=True)
class Principal:
    actor: str
    scopes: frozenset[str]


def generate_token() -> str:
    return f"htk_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
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
    try:
        algorithm, encoded_salt, encoded_digest = encoded_hash.split(":", 2)
        if algorithm != "scrypt-v1":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt)
        expected = base64.urlsafe_b64decode(encoded_digest)
        actual = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1)
    except (ValueError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


def token_prefix(token: str) -> str:
    return token[:12]


def make_auth_dependency(settings: Settings, connection: Callable[..., Any]) -> Callable[..., None]:
    connection_dependency = Depends(connection)

    def check_auth(
        request: Request,
        credentials: BearerCredentials,
        conn: sqlite3.Connection = connection_dependency,
    ) -> None:
        principal: Principal
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
        token = credentials.credentials
        if settings.auth_token and hmac.compare_digest(token, settings.auth_token):
            principal = Principal("master", CLIENT_SCOPES)
        else:
            scoped_principal = _find_principal(conn, token)
            if scoped_principal is None:
                raise HTTPException(status_code=401, detail="Invalid authorization token")
            principal = scoped_principal
        request.state.tracker_principal = principal
        if _scope_for_request(request) not in principal.scopes:
            raise HTTPException(status_code=403, detail="Credential lacks required scope")

    return check_auth


def principal_for_request(request: Request) -> Principal:
    principal = getattr(request.state, "tracker_principal", None)
    if not isinstance(principal, Principal):
        raise RuntimeError("Tracker principal was not initialized")
    return principal


def _find_principal(conn: sqlite3.Connection, token: str) -> Principal | None:
    for record in store.list_active_agent_tokens(
        conn, token_prefix(token), datetime.now(UTC).isoformat()
    ):
        if verify_token_hash(token, record["token_hash"]):
            return Principal(record["actor"], frozenset(record["scopes"]))
    return None


def _scope_for_request(request: Request) -> str:
    path = request.url.path.rstrip("/")
    root_path = str(request.scope.get("root_path", "")).rstrip("/")
    if root_path and path.startswith(f"{root_path}/"):
        path = path[len(root_path) :]
    if request.method == "GET":
        return "read"
    if path == "/api/v1/git-closures":
        return "git:close"
    if path == "/api/v1/repos":
        return "repo:manage"
    if request.method == "PATCH" and path.count("/") == 4:
        return "repo:manage"
    if path.endswith("/comments"):
        return "comment:create"
    if path.endswith("/todos"):
        return "todo:create"
    return "todo:update"
