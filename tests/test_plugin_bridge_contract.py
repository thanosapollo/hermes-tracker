"""Ensure the plugin bridge fixture stays aligned with the generated API contract."""

import json
from pathlib import Path

from conftest import AUTH_HEADERS

FIXTURE = Path(__file__).parents[1] / "docs" / "contracts" / "plugin-bridge-v1.json"


def test_plugin_bridge_fixture_matches_openapi_and_replays(client) -> None:
    contract = json.loads(FIXTURE.read_text())
    schema = client.get("/openapi.json").json()
    for operation in contract["idempotent_mutations"]:
        path = operation["path"]
        assert path in schema["paths"]
        parameters = schema["paths"][path]["post"]["parameters"]
        assert {parameter["name"] for parameter in parameters} >= {"Idempotency-Key", "X-Actor"}
        response_headers = schema["paths"][path]["post"]["responses"][str(operation["status"])][
            "headers"
        ]
        assert "X-Mutation-ID" in response_headers

    backfill = contract["external_reference"]["backfill_board_identity"]
    path = backfill["path"]
    assert path in schema["paths"]
    assert schema["paths"][path][backfill["method"].lower()]["responses"]["200"]

    assert (
        client.post(
            "/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS
        ).status_code
        == 201
    )
    example = contract["create_todo_example"]
    headers = {**AUTH_HEADERS, **example["headers"]}
    first = client.post(example["path"], json=example["body"], headers=headers)
    replay = client.post(example["path"], json=example["body"], headers=headers)
    assert first.status_code == replay.status_code == example["status"]
    assert first.json() == replay.json()
    assert first.headers["X-Mutation-ID"] == replay.headers["X-Mutation-ID"]
