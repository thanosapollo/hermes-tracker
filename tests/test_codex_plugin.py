"""Contract checks for the bundled Codex MCP plugin."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

PLUGIN = Path(__file__).parents[1] / "plugins" / "codex-hermes-tracker"
SERVER = PLUGIN / "scripts" / "hermes_tracker_mcp.py"


def _module():
    spec = importlib.util.spec_from_file_location("hermes_tracker_mcp", SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_manifest_and_mcp_config_are_wired() -> None:
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
    config = json.loads((PLUGIN / ".mcp.json").read_text())

    assert manifest["name"] == "codex-hermes-tracker"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert config["mcpServers"]["codex-hermes-tracker"]["args"] == [
        "./scripts/hermes_tracker_mcp.py"
    ]


def test_comment_enforces_codex_identity_and_idempotency(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_TRACKER_ACTOR", raising=False)
    module = _module()
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Any:
        calls.append((method, url, headers, body))
        return {"id": 1, "author": module.ACTOR, "body": "Finding", "created_at": "now"}

    client = module.TrackerClient("secret", transport)
    result = client.call(
        "tracker_comment",
        {
            "repo_slug": "proj",
            "number": 3,
            "body": "Finding",
            "_idempotency_key": "invocation-1",
        },
    )

    assert result["author"] == "codex"
    payload = calls[0][3]
    assert payload is not None
    assert json.loads(payload) == {"author": "codex", "body": "Finding"}
    assert calls[0][2]["X-Actor"] == "codex"
    assert calls[0][2]["Idempotency-Key"] == "invocation-1"
    assert "Host" not in calls[0][2]


def test_mcp_lists_tools_and_dispatches_reads() -> None:
    module = _module()
    client = module.TrackerClient("secret", lambda *_args: [{"slug": "proj"}])

    listed = module.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, client)
    called = module.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tracker_list_repositories", "arguments": {}},
        },
        client,
    )

    assert {tool["name"] for tool in listed["result"]["tools"]} >= {
        "tracker_get",
        "tracker_create",
        "tracker_comment",
        "tracker_close",
    }
    assert json.loads(called["result"]["content"][0]["text"]) == [{"slug": "proj"}]


def test_load_token_uses_private_credential_file(tmp_path, monkeypatch) -> None:
    module = _module()
    token_file = tmp_path / "token"
    token_file.write_text("current-token\n")
    token_file.chmod(0o600)
    monkeypatch.delenv("HERMES_TRACKER_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_TRACKER_TOKEN_FILE", str(token_file))

    assert module.load_token() == "current-token"


def test_load_token_fails_without_explicit_credential(tmp_path, monkeypatch) -> None:
    module = _module()
    monkeypatch.delenv("HERMES_TRACKER_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_TRACKER_TOKEN_FILE", str(tmp_path / "missing"))

    with pytest.raises(RuntimeError, match="HERMES_TRACKER_TOKEN"):
        module.load_token()


def test_load_token_rejects_empty_credential_file(tmp_path, monkeypatch) -> None:
    module = _module()
    token_file = tmp_path / "token"
    token_file.write_text("")
    token_file.chmod(0o600)
    monkeypatch.delenv("HERMES_TRACKER_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_TRACKER_TOKEN_FILE", str(token_file))

    with pytest.raises(RuntimeError, match="HERMES_TRACKER_TOKEN"):
        module.load_token()


def test_client_accepts_explicit_endpoint_host_and_actor(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_TRACKER_URL", "https://tracker.example.org")
    monkeypatch.setenv("HERMES_TRACKER_HOST", "internal.example.org")
    monkeypatch.setenv("HERMES_TRACKER_ACTOR", "review-bot")
    module = _module()
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Any:
        calls.append((method, url, headers, body))
        return {"ok": True}

    client = module.TrackerClient("secret", transport)
    client.request("GET", "/api/v1/repos")

    assert module.ACTOR == "review-bot"
    assert calls[0][1] == "https://tracker.example.org/api/v1/repos"
    assert calls[0][2]["Host"] == "internal.example.org"


@pytest.mark.parametrize(
    "url",
    [
        "http://tracker.example.org",
        "https://user:password@tracker.example.org",
        "https://tracker.example.org?query=yes",
        "https://tracker.example.org#fragment",
        "https://tracker.example.org:99999",
    ],
)
def test_client_rejects_unsafe_endpoint(monkeypatch, url: str) -> None:
    monkeypatch.setenv("HERMES_TRACKER_URL", url)
    module = _module()

    with pytest.raises(RuntimeError, match="HERMES_TRACKER_URL"):
        module.TrackerClient("secret")
