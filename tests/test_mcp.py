"""Protocol-level tests for the Tracker stdio MCP wrapper."""

from __future__ import annotations

import io
import json
import urllib.error

from hermes_tracker import mcp


class _Response:
    def __init__(self, body: str = "{}") -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode()


def test_tools_expose_complete_bounded_schemas() -> None:
    tools = {tool["name"]: tool["inputSchema"] for tool in mcp._tools()}

    create = tools["tracker_create"]
    assert create["required"] == ["repo", "title"]
    assert create["additionalProperties"] is False
    assert create["properties"]["title"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
    }
    assert create["properties"]["description"] == {"type": ["string", "null"]}
    assert create["properties"]["priority"] == {
        "type": "integer",
        "minimum": -1000,
        "maximum": 1000,
    }
    assert create["properties"]["assignee"] == {"type": ["string", "null"]}
    assert create["properties"]["acceptance_criteria"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert create["properties"]["verification_commands"] == {
        "type": "array",
        "items": {"type": "string"},
    }

    update = tools["tracker_update"]
    assert update["required"] == ["repo", "number"]
    assert update["additionalProperties"] is False
    assert set(update["properties"]) == {
        "repo",
        "number",
        "title",
        "description",
        "priority",
        "assignee",
        "acceptance_criteria",
        "verification_commands",
    }
    assert update["properties"]["priority"] == {
        "type": ["integer", "null"],
        "minimum": -1000,
        "maximum": 1000,
    }

    close = tools["tracker_close"]
    assert close["required"] == ["repo", "number", "verification_output"]
    assert close["additionalProperties"] is False
    assert close["properties"]["closing_commit"] == {
        "type": ["string", "null"],
        "maxLength": 64,
    }

    link = tools["tracker_link"]
    assert link["properties"]["link_type"]["enum"] == [
        "blocks",
        "blocked-by",
        "depends-on",
        "duplicate-of",
        "related-to",
        "supersedes",
        "superseded-by",
    ]
    assert tools["tracker_list"]["properties"]["status_filter"] == {
        "type": "string",
        "enum": ["open", "active", "blocked", "done"],
    }
    assert all(schema["additionalProperties"] is False for schema in tools.values())


def test_call_rejects_undeclared_arguments_without_leaking_the_token(monkeypatch) -> None:
    def should_not_be_called(*args, **kwargs):
        raise AssertionError("unexpected HTTP request")

    monkeypatch.setattr(mcp.urllib.request, "urlopen", should_not_be_called)

    result = mcp._call(
        "tracker_create",
        {"repo": "demo", "title": "Add schemas", "unsupported": "value"},
        "private-token",
        "http://tracker.invalid",
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "Invalid Tracker tool arguments"}],
    }
    assert "private-token" not in json.dumps(result)


def test_call_sends_declared_optional_fields_and_get_filter(monkeypatch) -> None:
    requests = []

    def capture(request, timeout):
        assert timeout == 15
        requests.append(request)
        return _Response('{"ok": true}')

    monkeypatch.setattr(mcp.urllib.request, "urlopen", capture)

    create = mcp._call(
        "tracker_create",
        {
            "repo": "demo",
            "title": "Add schemas",
            "description": "REST parity",
            "priority": 2,
            "assignee": "worker",
            "acceptance_criteria": ["tests pass"],
            "verification_commands": ["pytest -q"],
        },
        "private-token",
        "http://tracker.invalid",
    )
    update = mcp._call(
        "tracker_update",
        {"repo": "demo", "number": 3, "priority": -1, "assignee": None},
        "private-token",
        "http://tracker.invalid",
    )
    close = mcp._call(
        "tracker_close",
        {"repo": "demo", "number": 3, "verification_output": "passed", "closing_commit": "abc123"},
        "private-token",
        "http://tracker.invalid",
    )
    listed = mcp._call(
        "tracker_list",
        {"repo": "demo", "status_filter": "active"},
        "private-token",
        "http://tracker.invalid",
    )
    rejected = mcp._call(
        "tracker_list",
        {"repo": "demo", "status_filter": "in review"},
        "private-token",
        "http://tracker.invalid",
    )

    expected_result = {"content": [{"type": "text", "text": '{"ok": true}'}]}
    assert [create, update, close, listed] == [expected_result] * 4
    assert rejected == {
        "isError": True,
        "content": [{"type": "text", "text": "Invalid Tracker tool arguments"}],
    }
    assert json.loads(requests[0].data) == {
        "title": "Add schemas",
        "description": "REST parity",
        "priority": 2,
        "assignee": "worker",
        "acceptance_criteria": ["tests pass"],
        "verification_commands": ["pytest -q"],
    }
    assert json.loads(requests[1].data) == {"priority": -1, "assignee": None}
    assert json.loads(requests[2].data) == {
        "verification_output": "passed",
        "closing_commit": "abc123",
    }
    assert requests[3].data is None
    assert (
        requests[3].full_url
        == "http://tracker.invalid/api/v1/repos/demo/todos?status_filter=active"
    )


def test_unknown_tool_is_an_mcp_error_without_leaking_the_token() -> None:
    result = mcp._call("not-a-tool", {}, "private-token", "http://tracker.invalid")

    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "Unknown Tracker tool"}]
    assert "private-token" not in json.dumps(result)


def test_rest_failure_is_reported_without_leaking_the_token(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(mcp.urllib.request, "urlopen", unavailable)

    result = mcp._call(
        "tracker_get", {"repo": "demo", "number": 1}, "private-token", "http://tracker.invalid"
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "Tracker is unavailable"}],
    }
    assert "private-token" not in json.dumps(result)


def test_stdio_protocol_initializes_and_lists_tools(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_TRACKER_TOKEN", "private-token")
    monkeypatch.setattr(
        mcp.sys,
        "stdin",
        io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(mcp.sys, "stdout", output)

    assert mcp.main() == 0

    initialized, listed = [json.loads(line) for line in output.getvalue().splitlines()]
    assert initialized["result"]["protocolVersion"] == "2024-11-05"
    assert {tool["name"] for tool in listed["result"]["tools"]} == set(mcp._TOOL_PATHS)
