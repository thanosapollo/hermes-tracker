"""Small stdio MCP wrapper for Tracker's versioned REST API."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_TOOL_PATHS = {
    "tracker_list": ("GET", "/repos/{repo}/todos"),
    "tracker_get": ("GET", "/repos/{repo}/todos/{number}"),
    "tracker_create": ("POST", "/repos/{repo}/todos"),
    "tracker_update": ("PATCH", "/repos/{repo}/todos/{number}"),
    "tracker_comment": ("POST", "/repos/{repo}/todos/{number}/comments"),
    "tracker_link": ("POST", "/repos/{repo}/todos/{number}/links"),
    "tracker_claim": ("POST", "/repos/{repo}/todos/{number}/claim"),
    "tracker_close": ("POST", "/repos/{repo}/todos/{number}/close"),
}

_STRING: dict[str, Any] = {"type": "string"}
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_POSITIVE_INTEGER: dict[str, Any] = {"type": "integer", "minimum": 1}
_TODO_TITLE: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 500}
_NULLABLE_TODO_TITLE: dict[str, Any] = {
    "type": ["string", "null"],
    "minLength": 1,
    "maxLength": 500,
}
_PRIORITY: dict[str, Any] = {"type": "integer", "minimum": -1000, "maximum": 1000}
_NULLABLE_PRIORITY: dict[str, Any] = {
    "type": ["integer", "null"],
    "minimum": -1000,
    "maximum": 1000,
}
_STRING_LIST: dict[str, Any] = {"type": "array", "items": _STRING}
_NULLABLE_STRING_LIST: dict[str, Any] = {"type": ["array", "null"], "items": _STRING}
_STATUS_FILTER: dict[str, Any] = {
    "type": "string",
    "enum": ["open", "active", "blocked", "done"],
}
_TOOL_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "tracker_list": {"repo": {"type": "string", "minLength": 1}, "status_filter": _STATUS_FILTER},
    "tracker_get": {"repo": {"type": "string", "minLength": 1}, "number": _POSITIVE_INTEGER},
    "tracker_create": {
        "repo": {"type": "string", "minLength": 1},
        "title": _TODO_TITLE,
        "description": _NULLABLE_STRING,
        "priority": _PRIORITY,
        "assignee": _NULLABLE_STRING,
        "acceptance_criteria": _STRING_LIST,
        "verification_commands": _STRING_LIST,
    },
    "tracker_update": {
        "repo": {"type": "string", "minLength": 1},
        "number": _POSITIVE_INTEGER,
        "title": _NULLABLE_TODO_TITLE,
        "description": _NULLABLE_STRING,
        "priority": _NULLABLE_PRIORITY,
        "assignee": _NULLABLE_STRING,
        "acceptance_criteria": _NULLABLE_STRING_LIST,
        "verification_commands": _NULLABLE_STRING_LIST,
    },
    "tracker_comment": {
        "repo": {"type": "string", "minLength": 1},
        "number": _POSITIVE_INTEGER,
        "author": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "minLength": 1},
    },
    "tracker_link": {
        "repo": {"type": "string", "minLength": 1},
        "number": _POSITIVE_INTEGER,
        "target_repo_slug": _STRING,
        "target_number": _POSITIVE_INTEGER,
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
    "tracker_claim": {
        "repo": {"type": "string", "minLength": 1},
        "number": _POSITIVE_INTEGER,
        "assignee": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "tracker_close": {
        "repo": {"type": "string", "minLength": 1},
        "number": _POSITIVE_INTEGER,
        "verification_output": {"type": "string", "minLength": 1},
        "closing_commit": {"type": ["string", "null"], "maxLength": 40},
    },
}
_REQUIRED_ARGUMENTS = {
    "tracker_list": ["repo"],
    "tracker_get": ["repo", "number"],
    "tracker_create": ["repo", "title"],
    "tracker_update": ["repo", "number"],
    "tracker_comment": ["repo", "number", "author", "body"],
    "tracker_link": ["repo", "number", "target_repo_slug", "target_number", "link_type"],
    "tracker_claim": ["repo", "number", "assignee"],
    "tracker_close": ["repo", "number", "verification_output"],
}
_PATH_ARGUMENTS = {
    "tracker_list": ("repo",),
    "tracker_get": ("repo", "number"),
    "tracker_create": ("repo",),
    "tracker_update": ("repo", "number"),
    "tracker_comment": ("repo", "number"),
    "tracker_link": ("repo", "number"),
    "tracker_claim": ("repo", "number"),
    "tracker_close": ("repo", "number"),
}
_QUERY_ARGUMENTS = {"tracker_list": ("status_filter",)}


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": "Call the corresponding Tracker REST operation.",
            "inputSchema": {
                "type": "object",
                "properties": _TOOL_SCHEMAS[name],
                "required": _REQUIRED_ARGUMENTS[name],
                "additionalProperties": False,
            },
        }
        for name in _TOOL_PATHS
    ]


def _arguments_match_schema(name: str, arguments: dict[str, Any]) -> bool:
    properties = _TOOL_SCHEMAS[name]
    has_unexpected_argument = bool(set(arguments) - set(properties))
    has_missing_required_argument = any(key not in arguments for key in _REQUIRED_ARGUMENTS[name])
    if has_unexpected_argument or has_missing_required_argument:
        return False
    return all(_value_matches_schema(value, properties[key]) for key, value in arguments.items())


def _value_matches_schema(value: Any, schema: dict[str, Any]) -> bool:
    allowed_types = schema["type"]
    if isinstance(allowed_types, str):
        allowed_types = [allowed_types]
    if value is None:
        return "null" in allowed_types
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        if "string" not in allowed_types:
            return False
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
    elif isinstance(value, int):
        if "integer" not in allowed_types:
            return False
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    elif isinstance(value, list):
        if "array" not in allowed_types:
            return False
        item_schema = schema["items"]
        if not all(_value_matches_schema(item, item_schema) for item in value):
            return False
    else:
        return False
    return "enum" not in schema or value in schema["enum"]


def _call(name: str, arguments: dict[str, Any], token: str, base_url: str) -> dict[str, Any]:
    tool = _TOOL_PATHS.get(name)
    if tool is None:
        return {"isError": True, "content": [{"type": "text", "text": "Unknown Tracker tool"}]}
    if not _arguments_match_schema(name, arguments):
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Invalid Tracker tool arguments"}],
        }
    method, template = tool
    path_arguments = {key: arguments[key] for key in _PATH_ARGUMENTS[name]}
    path = template.format(**path_arguments)
    query_arguments = {
        key: arguments[key] for key in _QUERY_ARGUMENTS.get(name, ()) if key in arguments
    }
    query = urllib.parse.urlencode(query_arguments)
    url = f"{base_url.rstrip('/')}/api/v1{path}"
    if query:
        url = f"{url}?{query}"
    excluded = set(path_arguments) | set(query_arguments)
    body = {key: value for key, value in arguments.items() if key not in excluded}
    data = None if method == "GET" else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = response.read().decode()
    except urllib.error.HTTPError as exc:
        result = f"Tracker returned HTTP {exc.code}"
        return {"isError": True, "content": [{"type": "text", "text": result}]}
    except urllib.error.URLError:
        return {"isError": True, "content": [{"type": "text", "text": "Tracker is unavailable"}]}
    return {"content": [{"type": "text", "text": result}]}


def main() -> int:
    token = os.environ.get("HERMES_TRACKER_TOKEN")
    base_url = os.environ.get("HERMES_TRACKER_BASE_URL", "http://127.0.0.1:8000")
    if not token:
        print("HERMES_TRACKER_TOKEN is required", file=sys.stderr)
        return 2
    for line in sys.stdin:
        try:
            request = json.loads(line)
            method = request["method"]
            request_id = request.get("id")
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                }
            elif method == "tools/list":
                result = {"tools": _tools()}
            elif method == "tools/call":
                params = request["params"]
                result = _call(params["name"], dict(params.get("arguments", {})), token, base_url)
            else:
                raise ValueError("unsupported method")
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
        except (KeyError, TypeError, ValueError) as exc:
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": str(exc)}}
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
