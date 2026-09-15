"""Exercise real TODO and comment rendering through the HTTP boundary."""

from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from test_markdown import ParsedHTML

BODY = (
    "commit:AbC1234 tree:123abcd bare abcdef0 date 20260915\n\n"
    "`tree:7654321` \\commit:7654321\n\n"
    "[chosen tree](https://example.org/tree/?id=123abcd)\n\n"
    "<script>alert(1)</script>"
)


def test_description_and_comment_links_and_stable_permalinks(client: TestClient) -> None:
    assert (
        client.post(
            "/api/v1/repos", headers=AUTH_HEADERS, json={"slug": "sample", "name": "Sample"}
        ).status_code
        == 201
    )
    created = client.post(
        "/api/v1/repos/sample/todos",
        headers={**AUTH_HEADERS, "Idempotency-Key": "reference-todo"},
        json={"title": "Typed references", "description": BODY},
    )
    assert created.status_code == 201
    # Consume a comment ID on another TODO: permalinks must use stored IDs,
    # not the position within this page's comment list.
    assert (
        client.post(
            "/api/v1/repos/sample/todos",
            headers={**AUTH_HEADERS, "Idempotency-Key": "other-todo"},
            json={"title": "Another discussion"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/repos/sample/todos/2/comments",
            headers={**AUTH_HEADERS, "Idempotency-Key": "other-comment"},
            json={"body": "Elsewhere"},
        ).status_code
        == 201
    )
    comment = client.post(
        "/api/v1/repos/sample/todos/1/comments",
        headers={**AUTH_HEADERS, "Idempotency-Key": "reference-comment"},
        json={"body": BODY},
    )
    assert comment.status_code == 201
    comment_id = comment.json()["id"]
    fragment = f"comment-{comment_id}"

    for pass_number in range(2):
        response = client.get(f"/r/sample/1#{fragment}")
        assert response.status_code == 200
        parsed = ParsedHTML(response.text)
        hrefs = [anchor.get("href") for anchor in parsed.anchors]
        assert hrefs.count("https://git.example.org/sample/commit/?id=AbC1234") == 2
        assert hrefs.count("https://git.example.org/sample/tree/?id=123abcd") == 0
        assert response.text.count("tree:123abcd") == 2
        assert hrefs.count("https://example.org/tree/?id=123abcd") == 2
        assert not any("?id=abcdef0" in (href or "") for href in hrefs)
        assert not any("?id=20260915" in (href or "") for href in hrefs)
        assert not any("?id=7654321" in (href or "") for href in hrefs)
        ids = [attrs["id"] for _, attrs in parsed.elements if "id" in attrs]
        assert ids.count(fragment) == 1
        assert len(ids) == len(set(ids))
        permalink = next(a for a in parsed.anchors if a.get("href") == f"#{fragment}")
        assert permalink.get("aria-label") == (
            f"Permalink to comment {comment_id}: {comment.json()['created_at']}"
        )
        assert "tabindex" not in permalink
        assert all(tag != "script" for tag, _ in parsed.elements)
        if pass_number == 0:
            assert (
                client.post(
                    "/api/v1/repos/sample/todos/1/comments",
                    headers={**AUTH_HEADERS, "Idempotency-Key": "later-comment"},
                    json={"body": "Later comment"},
                ).status_code
                == 201
            )

    stored = client.get("/api/v1/repos/sample/todos/1", headers=AUTH_HEADERS).json()
    assert stored["description"] == BODY
    assert stored["comments"][0]["body"] == BODY
