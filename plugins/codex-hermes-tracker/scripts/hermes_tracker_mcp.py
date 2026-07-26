#!/usr/bin/env python3
"""Expose Hermes Tracker's HTTP API as Codex MCP tools."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

ACTOR = os.environ.get("HERMES_TRACKER_ACTOR", "codex").strip() or "codex"
DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_TOKEN_FILE = "~/.config/hermes-tracker/token"
SESSION_ID = uuid.uuid4()
JsonObject = dict[str, Any]
Transport = Callable[[str, str, Mapping[str, str], bytes | None], Any]


def _object_schema(properties: JsonObject, required: list[str]) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


REPO = {"type": "string", "minLength": 1, "description": "Repository slug."}
NUMBER = {"type": "integer", "minimum": 1, "description": "Repository-local TODO number."}
BODY = {"type": "string", "minLength": 1, "description": "Public-safe Markdown text."}

TOOLS = [
    {
        "name": "tracker_list_repositories",
        "description": "List repositories known to Hermes Tracker.",
        "inputSchema": _object_schema({}, []),
    },
    {
        "name": "tracker_list",
        "description": "List TODOs in a repository, optionally filtered by status.",
        "inputSchema": _object_schema(
            {"repo_slug": REPO, "status": {"type": "string"}}, ["repo_slug"]
        ),
    },
    {
        "name": "tracker_get",
        "description": "Read one TODO with comments, links, and Hermes Kanban references.",
        "inputSchema": _object_schema(
            {"repo_slug": REPO, "number": NUMBER}, ["repo_slug", "number"]
        ),
    },
    {
        "name": "tracker_create",
        "description": "Create a durable TODO. Retries are idempotent.",
        "inputSchema": _object_schema(
            {
                "repo_slug": REPO,
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "description": {"type": ["string", "null"]},
                "priority": {"type": "integer", "minimum": -1000, "maximum": 1000},
                "assignee": {"type": ["string", "null"]},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "verification_commands": {"type": "array", "items": {"type": "string"}},
            },
            ["repo_slug", "title"],
        ),
    },
    {
        "name": "tracker_update",
        "description": "Update mutable TODO fields without changing its status.",
        "inputSchema": _object_schema(
            {
                "repo_slug": REPO,
                "number": NUMBER,
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "description": {"type": ["string", "null"]},
                "priority": {"type": "integer", "minimum": -1000, "maximum": 1000},
                "assignee": {"type": ["string", "null"]},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "verification_commands": {"type": "array", "items": {"type": "string"}},
            },
            ["repo_slug", "number"],
        ),
    },
    {
        "name": "tracker_claim",
        "description": "Claim a TODO for an assignee. Retries are idempotent.",
        "inputSchema": _object_schema(
            {
                "repo_slug": REPO,
                "number": NUMBER,
                "assignee": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            ["repo_slug", "number", "assignee"],
        ),
    },
    {
        "name": "tracker_comment",
        "description": "Append a public comment using the configured actor.",
        "inputSchema": _object_schema(
            {"repo_slug": REPO, "number": NUMBER, "body": BODY},
            ["repo_slug", "number", "body"],
        ),
    },
    {
        "name": "tracker_link",
        "description": "Create a typed link from one tracker TODO to another.",
        "inputSchema": _object_schema(
            {
                "repo_slug": REPO,
                "number": NUMBER,
                "target_repo_slug": REPO,
                "target_number": NUMBER,
                "link_type": {
                    "type": "string",
                    "enum": [
                        "blocks",
                        "blocked-by",
                        "depends-on",
                        "duplicate-of",
                        "related-to",
                        "supersedes",
                        "superseded-by",
                    ],
                },
            },
            ["repo_slug", "number", "target_repo_slug", "target_number", "link_type"],
        ),
    },
    {
        "name": "tracker_close",
        "description": "Close a TODO with public verification evidence. Retries are idempotent.",
        "inputSchema": _object_schema(
            {
                "repo_slug": REPO,
                "number": NUMBER,
                "verification_output": BODY,
                "closing_commit": {"type": ["string", "null"], "maxLength": 64},
            },
            ["repo_slug", "number", "verification_output"],
        ),
    },
]


def load_token() -> str:
    token = os.environ.get("HERMES_TRACKER_TOKEN", "").strip()
    if token:
        return token
    token_file = Path(os.environ.get("HERMES_TRACKER_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()
    if token_file.is_file():
        if token_file.stat().st_mode & 0o077:
            raise RuntimeError(f"Tracker credential file must have mode 0600: {token_file}")
        lines = token_file.read_text().splitlines()
        token = lines[0].strip() if lines else ""
        if token:
            return token
    raise RuntimeError(
        "Tracker credential is unavailable; set HERMES_TRACKER_TOKEN "
        "or provide a mode-0600 token file"
    )


def default_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Any:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tracker returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Tracker connection failed: {exc.reason}") from exc


def normalize_base_url(raw: str) -> str:
    """Validate an API origin and return it without a trailing slash."""
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("HERMES_TRACKER_URL is invalid") from exc
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RuntimeError("HERMES_TRACKER_URL must contain a host without credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError("HERMES_TRACKER_URL must not contain a query or fragment")
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise RuntimeError("HERMES_TRACKER_URL must use HTTPS unless it targets loopback")
    return raw.rstrip("/")


class TrackerClient:
    def __init__(self, token: str, transport: Transport = default_transport) -> None:
        self.base_url = normalize_base_url(os.environ.get("HERMES_TRACKER_URL", DEFAULT_URL))
        self.host = os.environ.get("HERMES_TRACKER_HOST", "").strip()
        self.token = token
        self.transport = transport

    def request(
        self,
        method: str,
        path: str,
        body: JsonObject | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if self.host:
            headers["Host"] = self.host
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, separators=(",", ":")).encode()
        if idempotency_key:
            headers.update({"Idempotency-Key": idempotency_key, "X-Actor": ACTOR})
        return self.transport(method, self.base_url + path, headers, payload)

    def todo_path(self, args: Mapping[str, Any]) -> str:
        repo = quote(str(args["repo_slug"]), safe="")
        return f"/api/v1/repos/{repo}/todos/{int(args['number'])}"

    def call(self, name: str, args: JsonObject) -> Any:
        handlers = {
            "tracker_list_repositories": self.list_repositories,
            "tracker_list": self.list_todos,
            "tracker_get": self.get_todo,
            "tracker_create": self.create_todo,
            "tracker_update": self.update_todo,
            "tracker_claim": self.claim_todo,
            "tracker_comment": self.comment,
            "tracker_link": self.link,
            "tracker_close": self.close,
        }
        if name not in handlers:
            raise ValueError(f"Unknown tracker tool: {name}")
        return handlers[name](args)

    def list_repositories(self, _args: JsonObject) -> Any:
        return self.request("GET", "/api/v1/repos")

    def list_todos(self, args: JsonObject) -> Any:
        repo = quote(str(args["repo_slug"]), safe="")
        query = urlencode({"status_filter": args["status"]}) if "status" in args else ""
        suffix = f"?{query}" if query else ""
        return self.request("GET", f"/api/v1/repos/{repo}/todos{suffix}")

    def get_todo(self, args: JsonObject) -> Any:
        return self.request("GET", self.todo_path(args))

    def create_todo(self, args: JsonObject) -> Any:
        repo = quote(str(args["repo_slug"]), safe="")
        body = {
            key: value
            for key, value in args.items()
            if not key.startswith("_") and key != "repo_slug"
        }
        return self.request(
            "POST",
            f"/api/v1/repos/{repo}/todos",
            body,
            idempotency_key=args["_idempotency_key"],
        )

    def update_todo(self, args: JsonObject) -> Any:
        path = self.todo_path(args)
        body = {key: value for key, value in args.items() if key not in {"repo_slug", "number"}}
        if not body:
            raise ValueError("tracker_update requires at least one mutable field")
        return self.request("PATCH", path, body)

    def claim_todo(self, args: JsonObject) -> Any:
        return self.request(
            "POST",
            self.todo_path(args) + "/claim",
            {"assignee": args["assignee"]},
            idempotency_key=args["_idempotency_key"],
        )

    def comment(self, args: JsonObject) -> Any:
        body = {"author": ACTOR, "body": args["body"]}
        return self.request(
            "POST",
            self.todo_path(args) + "/comments",
            body,
            idempotency_key=args["_idempotency_key"],
        )

    def link(self, args: JsonObject) -> Any:
        body = {
            "target_repo_slug": args["target_repo_slug"],
            "target_number": args["target_number"],
            "link_type": args["link_type"],
        }
        return self.request(
            "POST",
            self.todo_path(args) + "/links",
            body,
            idempotency_key=args["_idempotency_key"],
        )

    def close(self, args: JsonObject) -> Any:
        body = {
            "verification_output": args["verification_output"],
            "closing_commit": args.get("closing_commit"),
        }
        return self.request(
            "POST",
            self.todo_path(args) + "/close",
            body,
            idempotency_key=args["_idempotency_key"],
        )


def tool_result(value: Any) -> JsonObject:
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, sort_keys=True)}]}


def dispatch(message: JsonObject, client: TrackerClient) -> JsonObject | None:
    if "id" not in message:
        return None
    request_id = message["id"]
    method = message.get("method")
    try:
        result: JsonObject
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "codex-hermes-tracker", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            name = str(params.get("name"))
            arguments = dict(params.get("arguments", {}))
            arguments["_idempotency_key"] = str(uuid.uuid5(SESSION_ID, f"{request_id}:{name}"))
            result = tool_result(client.call(name, arguments))
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
        }


def main() -> int:
    try:
        client = TrackerClient(load_token())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for line in sys.stdin:
        if not line.strip():
            continue
        response = dispatch(json.loads(line), client)
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
