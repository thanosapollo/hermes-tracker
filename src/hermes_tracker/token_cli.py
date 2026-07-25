"""Operator-local management of scoped Tracker credentials."""

from __future__ import annotations

import argparse
import json

from . import sqlite_store as store
from .auth import ALL_SCOPES, generate_token, hash_token, token_prefix
from .config import Settings
from .db import connect, init_schema


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-tracker-token")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--actor", required=True)
    issue.add_argument("--scopes", required=True, help="comma-separated explicit scope names")
    issue.add_argument("--expires-at")
    commands.add_parser("list")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--actor", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    conn = connect(Settings.from_env().db_path)
    init_schema(conn)
    if args.command == "issue":
        scopes = {scope for scope in args.scopes.split(",") if scope}
        invalid = scopes - ALL_SCOPES
        if not scopes or invalid:
            raise SystemExit(f"invalid scopes: {','.join(sorted(invalid)) or 'none'}")
        token = generate_token()
        record = store.insert_agent_token(
            conn,
            actor=args.actor,
            token_prefix=token_prefix(token),
            token_hash=hash_token(token),
            scopes=scopes,
            expires_at=args.expires_at,
        )
        print(json.dumps({**record, "token": token}, sort_keys=True))
        return 0
    if args.command == "list":
        print(json.dumps(store.list_agent_tokens(conn), sort_keys=True))
        return 0
    if args.command == "revoke":
        print(
            json.dumps(
                {"revoked": store.disable_agent_token(conn, actor=args.actor)}, sort_keys=True
            )
        )
        return 0
    raise AssertionError("unreachable")
