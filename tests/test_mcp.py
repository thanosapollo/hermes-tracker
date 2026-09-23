"""Real MCP protocol over memory streams, with the real authenticated HTTP backend."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp")
from mcp.client import Client, ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conftest import TEST_TOKEN
from hermes_tracker import sqlite_store
from hermes_tracker.app import create_app
from hermes_tracker.config import Settings
from hermes_tracker.mcp_server import TrackerHTTP, create_server, endpoint
from test_simple_backend import create_repo, create_todo, issue, issue_token


def adapter(app, token=TEST_TOKEN, transport=None, root=""):
    return create_server(
        TrackerHTTP(
            "http://tracker.test" + root + "/api/v1",
            "https://public.example" + root,
            token,
            transport=transport or httpx.ASGITransport(app=app),
        )
    )


async def call(client, name, arguments=None, error=None):
    response = await client.call_tool(name, arguments or {})
    assert response.is_error == (error is not None), response
    payload = response.structured_content
    assert payload is not None
    assert json.loads(response.content[0].text) == payload
    if error:
        assert payload["error"]["code"] == error
    return payload


def test_protocol_journey(client, connection_factory):
    create_repo(client)

    async def journey():
        async with Client(adapter(client.app)) as mcp:
            tools = (await mcp.list_tools()).tools
            assert {tool.name for tool in tools} == {
                "tracker_repos",
                "tracker_labels",
                "tracker_list",
                "tracker_get",
                "tracker_create",
                "tracker_update",
                "tracker_reopen",
                "tracker_comment",
            }
            update_tool = next(tool for tool in tools if tool.name == "tracker_update")
            assert {"repo", "number", "changes", "expected_revision", "operation_key"} <= set(
                update_tool.input_schema["required"]
            )
            assert update_tool.annotations.idempotent_hint
            for tool in tools:
                if tool.name in {"tracker_update", "tracker_reopen"}:
                    assert tool.annotations.destructive_hint is True
                    assert tool.annotations.read_only_hint is False
                    assert tool.annotations.idempotent_hint is True
                elif tool.name in {"tracker_create", "tracker_comment"}:
                    assert tool.annotations.destructive_hint is False
                    assert tool.annotations.read_only_hint is False
                    assert tool.annotations.idempotent_hint is True
                else:
                    assert tool.annotations.read_only_hint is True
            repos = await call(mcp, "tracker_repos")
            assert repos["items"][0]["slug"] == "source"
            labels = await call(mcp, "tracker_labels")
            assert any(label["name"] == "bug" for label in labels["labels"])
            args = {
                "repo": "source",
                "title": "MCP journey",
                "operation_key": "create",
                "description": "Public description",
                "labels": ["bug"],
            }
            created = await call(mcp, "tracker_create", args)
            replay = await call(mcp, "tracker_create", args)
            assert replay == created
            assert created["ref"] == "source#1"
            assert created["url"] == "https://public.example/r/source/1"
            assert "comments" not in created and "description" not in created
            await call(mcp, "tracker_create", {**args, "title": "Changed"}, error="conflict")
            searched = await call(
                mcp, "tracker_list", {"repo": "source", "q": "journey", "labels": ["bug"]}
            )
            assert [todo["ref"] for todo in searched["items"]] == ["source#1"]
            read = await call(mcp, "tracker_get", {"repo": "source", "number": 1})
            assert read["description"] == "Public description"
            assert read["comment_count"] == 0 and "comments" not in read
            update = {
                "repo": "source",
                "number": 1,
                "changes": {"assignee": "alice"},
                "expected_revision": read["revision"],
                "operation_key": "edit",
            }
            edited = await call(mcp, "tracker_update", update)
            assert edited["assignee"] == "alice"
            assert await call(mcp, "tracker_update", update) == edited
            await call(mcp, "tracker_update", {**update, "operation_key": "stale"}, error="stale")
            comment_args = {
                "repo": "source",
                "number": 1,
                "body": "Public note",
                "operation_key": "comment",
            }
            comment = await call(mcp, "tracker_comment", comment_args)
            assert await call(mcp, "tracker_comment", comment_args) == comment
            second = await call(mcp, "tracker_comment", {**comment_args, "operation_key": "next"})
            assert second["comment_id"] != comment["comment_id"]
            discussion = await call(
                mcp,
                "tracker_get",
                {"repo": "source", "number": 1, "include_comments": True, "comments_limit": 1},
            )
            assert discussion["comment_count"] == discussion["comments"]["total"] == 2
            assert discussion["comments"]["has_more"]
            assert discussion["comments"]["next_offset"] == 1
            assert discussion["comments"]["items"][0]["url"] == comment["url"]
            remainder = await call(
                mcp,
                "tracker_get",
                {"repo": "source", "number": 1, "include_comments": True, "comments_offset": 1},
            )
            assert remainder["comments"]["items"][0]["id"] == second["comment_id"]
            assert not remainder["comments"]["has_more"]
            # Git closure is performed only by the backend fixture, never an MCP tool.
            with connection_factory() as conn:
                sqlite_store.close_todo_from_git(
                    conn,
                    1,
                    commit_sha="a" * 40,
                    source_repo="source",
                    source_ref="refs/heads/main",
                    actor="hook",
                )
                conn.commit()
            closed = await call(mcp, "tracker_get", {"repo": "source", "number": 1})
            await call(
                mcp,
                "tracker_update",
                {**update, "operation_key": "closed-edit", "expected_revision": closed["revision"]},
                error="conflict",
            )
            reopen_args = {
                "repo": "source",
                "number": 1,
                "expected_revision": closed["revision"],
                "operation_key": "reopen",
            }
            reopened = await call(mcp, "tracker_reopen", reopen_args)
            assert reopened["status"] == "open"
            assert await call(mcp, "tracker_reopen", reopen_args) == reopened
            await call(mcp, "tracker_get", {"repo": "source", "number": 999}, error="not_found")
            await call(
                mcp,
                "tracker_create",
                {**args, "title": "   ", "operation_key": "invalid"},
                error="invalid",
            )
            await call(mcp, "tracker_get", {"repo": "..", "number": 1}, error="invalid")

    asyncio.run(journey())
    assert len(issue(client)["comments"]) == 2
    assert issue(client)["closing_commit"] is None


def test_root_paths_pagination_and_scopes(client, connection_factory, settings):
    create_repo(client)
    create_repo(client, "other")
    for title in ("One", "Two", "Three"):
        create_todo(client, title=title)
    issue_token(connection_factory, "readonly", "reader", ["read"])
    rooted = create_app(
        settings=Settings(db_path=settings.db_path, root_path="/tracker", auth_token=TEST_TOKEN),
        get_conn=connection_factory,
    )

    async def journey():
        async with Client(adapter(rooted, token="readonly", root="/tracker")) as mcp:
            repos = await call(mcp, "tracker_repos", {"limit": 1})
            assert repos["total"] == 2 and repos["has_more"]
            assert repos["items"][0]["url"].startswith("https://public.example/tracker/r/")
            assert len((await call(mcp, "tracker_repos", {"offset": 1}))["items"]) == 1
            todos = await call(mcp, "tracker_list", {"repo": "source", "limit": 2})
            assert todos["total"] == 3 and todos["next_offset"] == 2
            last = await call(mcp, "tracker_list", {"repo": "source", "offset": 2})
            assert [todo["number"] for todo in last["items"]] == [3]
            assert not last["has_more"] and last["next_offset"] is None
            empty = await call(mcp, "tracker_list", {"repo": "source", "offset": 100})
            assert empty["items"] == [] and empty["total"] == 3
            read = await call(mcp, "tracker_get", {"repo": "source", "number": 1})
            for name, args in [
                ("tracker_create", {"repo": "source", "title": "Denied"}),
                ("tracker_comment", {"repo": "source", "number": 1, "body": "Denied"}),
                (
                    "tracker_update",
                    {
                        "repo": "source",
                        "number": 1,
                        "changes": {"title": "Denied"},
                        "expected_revision": read["revision"],
                    },
                ),
                (
                    "tracker_reopen",
                    {"repo": "source", "number": 1, "expected_revision": read["revision"]},
                ),
            ]:
                await call(mcp, name, {**args, "operation_key": name}, error="forbidden")
        async with Client(adapter(rooted, token="invalid", root="/tracker")) as mcp:
            await call(mcp, "tracker_repos", error="unauthorized")

    asyncio.run(journey())


@pytest.mark.parametrize("operation", ["create", "update", "comment", "reopen"])
def test_lost_post_write_response_retries_safely(client, connection_factory, operation):
    create_repo(client)
    initial = create_todo(client)
    if operation == "reopen":
        with connection_factory() as conn:
            sqlite_store.close_todo_from_git(
                conn,
                1,
                commit_sha="a" * 40,
                source_repo="source",
                source_ref="refs/heads/main",
                actor="hook",
            )
            conn.commit()
        initial = issue(client)
    args = {
        "create": {"title": "Lost response"},
        "update": {
            "number": 1,
            "changes": {"title": "Lost response"},
            "expected_revision": initial["revision"],
        },
        "comment": {"number": 1, "body": "Lost response"},
        "reopen": {"number": 1, "expected_revision": initial["revision"]},
    }[operation]
    requests = []
    transport = httpx.ASGITransport(app=client.app)

    async def lose_once(request):
        requests.append(
            (request.content, request.headers["idempotency-key"], request.headers.get("if-match"))
        )
        response = await transport.handle_async_request(request)
        assert response.status_code in {200, 201}
        if len(requests) == 1:
            await response.aclose()
            raise httpx.ReadError("Private diagnostics must not escape", request=request)
        return response

    async def journey():
        async with Client(adapter(client.app, transport=httpx.MockTransport(lose_once))) as mcp:
            arguments = {"repo": "source", "operation_key": "lost", **args}
            error = await call(mcp, "tracker_" + operation, arguments, error="retryable")
            assert error["error"]["retryable"]
            assert "Private diagnostics" not in json.dumps(error)
            received = await call(mcp, "tracker_" + operation, arguments)
            replay = await call(mcp, "tracker_" + operation, arguments)
            assert received == replay

    asyncio.run(journey())
    assert requests[0] == requests[1] == requests[2]
    with connection_factory() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mutation_receipts WHERE idempotency_key='lost'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == (
            2 if operation == "create" else 1
        )
        assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == (
            1 if operation == "comment" else 0
        )


@pytest.mark.parametrize(
    ("status", "code"),
    [(302, "protocol"), (422, "invalid"), (429, "retryable"), (500, "retryable")],
)
def test_backend_errors_never_dump_response_or_credentials(client, status, code):
    secret = "private backend traceback Bearer credential"
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=secret))

    async def journey():
        async with Client(adapter(client.app, transport=transport)) as mcp:
            response = await call(mcp, "tracker_repos", error=code)
            assert secret not in json.dumps(response)

    asyncio.run(journey())


@pytest.mark.parametrize(
    "value",
    [
        "https://[bad",
        "https://host:bad",
        "https://host:0",
        "https://host:65536",
        "https://user:secret@host",
        "https://host?q=x",
        "https://host#x",
        "https://host/../root",
        "https://host/%2e%2e",
        "https://host\\evil",
        "https://ho st",
    ],
)
def test_endpoint_errors_are_safe(value):
    with pytest.raises(ValueError, match="absolute HTTP") as error:
        endpoint(value)
    assert value not in str(error.value)


def test_cli_environment_errors_do_not_echo_secrets(monkeypatch):
    monkeypatch.setenv("HERMES_TRACKER_MCP_TOKEN", "secret-token")
    response = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_tracker.mcp_server",
            "--api-url",
            "https://user:private-password@host/api/v1",
            "--public-url",
            "https://public.example",
        ],
        capture_output=True,
        text=True,
    )
    assert response.returncode == 2
    assert response.stdout == ""
    assert "private-password" not in response.stderr and "secret-token" not in response.stderr


def test_adapter_import_has_no_backend_or_database_side_effects(tmp_path):
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hermes_tracker.mcp_server; "
            "assert 'hermes_tracker.app' not in sys.modules; "
            "assert 'hermes_tracker.sqlite_store' not in sys.modules",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert list(tmp_path.iterdir()) == []


def test_stdio_entry_point_against_real_http(client):
    import uvicorn

    create_repo(client)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(client.app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started

            async def journey():
                parameters = StdioServerParameters(
                    command=str(Path(sys.executable).with_name("hermes-tracker-mcp")),
                    args=[],
                    env={
                        **os.environ,
                        "HERMES_TRACKER_MCP_TOKEN": TEST_TOKEN,
                        "HERMES_TRACKER_MCP_API_URL": f"http://127.0.0.1:{port}/api/v1",
                        "HERMES_TRACKER_MCP_PUBLIC_URL": "https://public.example/mount",
                    },
                )
                async with (
                    stdio_client(parameters) as (read, write),
                    ClientSession(read, write) as mcp,
                ):
                    initialized = await mcp.initialize()
                    assert initialized.server_info.name == "hermes-tracker"
                    assert len((await mcp.list_tools()).tools) == 8
                    created = await call(
                        mcp,
                        "tracker_create",
                        {
                            "repo": "source",
                            "title": "Stdio issue",
                            "operation_key": "stdio",
                        },
                    )
                    readback = await call(mcp, "tracker_get", {"repo": "source", "number": 1})
                    assert readback["revision"] == created["revision"]
                    assert readback["url"] == "https://public.example/mount/r/source/1"
                    await call(
                        mcp, "tracker_get", {"repo": "source", "number": 2}, error="not_found"
                    )

            asyncio.run(journey())
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_invalid_mcp_arguments_are_structured_without_echoing_input(client):
    async def journey():
        async with Client(adapter(client.app)) as mcp:
            for name, arguments in [
                ("tracker_get", {"repo": "source", "number": "private-secret"}),
                ("tracker_repos", {"private-secret": "ignored?"}),
                ("nonexistent-private-secret", {}),
                (
                    "tracker_update",
                    {"repo": "source", "number": 1, "changes": {"status": "closed"}},
                ),
                ("tracker_repos", {"limit": 0}),
                ("tracker_create", {"repo": "source", "title": "Missing operation key"}),
            ]:
                payload = await call(mcp, name, arguments, error="invalid")
                assert "private-secret" not in json.dumps(payload)

    asyncio.run(journey())
