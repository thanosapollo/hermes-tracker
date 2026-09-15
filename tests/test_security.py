"""HTTP regressions for public write boundaries and browser response policy."""

from __future__ import annotations

import base64
import hashlib
import re
from html import unescape
from xml.dom import minidom

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from hermes_tracker import sqlite_store as store
from hermes_tracker.app import create_app
from hermes_tracker.config import Settings
from test_simple_backend import issue_token

CATEGORIES = (
    "credential",
    "private filesystem path",
    "raw log or traceback",
    "hidden infrastructure",
)


def prohibited(category: str) -> str:
    """Assemble synthetic policy probes; never copy real private material."""
    return {
        "credential": "password" + "=" + "synthetic-not-a-secret",
        "private filesystem path": "/".join(("", "home", "synthetic", "fixture")),
        "raw log or traceback": "Traceback " + "(most recent call last):",
        "hidden infrastructure": "https://" + ".".join(("synthetic", "internal")),
    }[category]


def write(client, method: str, path: str, payload: dict, key: str = "fixture"):
    return client.request(
        method, path, json=payload, headers={**AUTH_HEADERS, "Idempotency-Key": key}
    )


def seed(client) -> tuple[dict, dict]:
    repo = write(client, "POST", "/api/v1/repos", {"slug": "project", "name": "Project"})
    todo = write(client, "POST", "/api/v1/repos/project/todos", {"title": "Original"})
    assert repo.status_code == todo.status_code == 201
    return repo.json(), todo.json()


@pytest.mark.parametrize("category", CATEGORIES)
@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    ("resource", "field"),
    [
        ("repos", "name"),
        ("repos", "description"),
        ("todos", "title"),
        ("todos", "description"),
        ("todos", "assignee"),
    ],
)
def test_public_prose_rejected_before_persistence(
    client, db_conn, category, method, resource, field
):
    original_repo, original_todo = seed(client)
    before = list(db_conn.iterdump())
    value = prohibited(category)
    if resource == "repos":
        path = "/api/v1/repos" if method == "POST" else "/api/v1/repos/project"
        payload = (
            {"slug": "new-project", "name": "New"} if method == "POST" else {"name": "Changed"}
        )
    else:
        path = "/api/v1/repos/project/todos" + ("/1" if method == "PATCH" else "")
        payload = {"title": "Changed", "priority": 8}
    payload[field] = value
    response = write(client, method, path, payload, "rejected")
    assert response.status_code == 422
    assert category in response.text
    assert value not in response.text
    assert list(db_conn.iterdump()) == before
    assert (
        client.get("/api/v1/repos", headers=AUTH_HEADERS).json()[0]["name"] == original_repo["name"]
    )
    assert client.get("/api/v1/repos/project/todos/1", headers=AUTH_HEADERS).json() == original_todo
    for path in ("/", "/r/project", "/r/project/1"):
        page = client.get(path)
        assert page.status_code == 200
        assert value not in page.text


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("category", CATEGORIES)
def test_navigation_url_public_policy(client, db_conn, method, category):
    seed(client)
    before = list(db_conn.iterdump())
    value = prohibited(category)
    url = value if category == "hidden infrastructure" else f"https://example.org/{value}"
    payload: dict = {"navigation": [{"label": "docs", "url": url}]}
    if method == "POST":
        payload.update(slug="new-project", name="New")
    path = "/api/v1/repos" + ("/project" if method == "PATCH" else "")
    response = write(client, method, path, payload)
    assert response.status_code == 422
    assert category in response.text
    assert value not in response.text
    assert list(db_conn.iterdump()) == before


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_structured_labels_keep_grammar_and_public_policy(client, db_conn, method):
    seed(client)
    before = list(db_conn.iterdump())
    path = "/api/v1/repos/project/todos" + ("/1" if method == "PATCH" else "")
    for label in ("synthetic" + ".internal", "not a label"):
        response = write(client, method, path, {"title": "Changed", "labels": [label]}, label)
        assert response.status_code == 422
        assert list(db_conn.iterdump()) == before


@pytest.mark.parametrize("category", CATEGORIES)
def test_comment_policy_still_rejects_without_echo(client, db_conn, category):
    seed(client)
    before = list(db_conn.iterdump())
    value = prohibited(category)
    response = write(client, "POST", "/api/v1/repos/project/todos/1/comments", {"body": value})
    assert response.status_code == 422
    assert category in response.text
    assert value not in response.text
    assert list(db_conn.iterdump()) == before


@pytest.mark.parametrize(
    "description", [None, "", "Ordinary prose: fix auth; see https://example.org/docs"]
)
def test_valid_create_update_and_public_display(client, description):
    repo = write(
        client,
        "POST",
        "/api/v1/repos",
        {
            "slug": "project",
            "name": "Project & tools",
            "description": description,
            "navigation": [{"label": "docs", "url": "https://example.org/docs"}],
        },
    )
    assert repo.status_code == 201
    todo = write(
        client,
        "POST",
        "/api/v1/repos/project/todos",
        {
            "title": "  Fix authentication — safely  ",
            "description": description,
            "assignee": "  Public Contributor  ",
            "labels": ["Bug", "docs"],
        },
    )
    assert todo.status_code == 201
    assert todo.json()["title"] == "Fix authentication — safely"
    assert todo.json()["assignee"] == "Public Contributor"
    assert todo.json()["labels"] == ["bug", "docs"]
    for path in ("/api/v1/repos/project", "/api/v1/repos/project/todos/1"):
        assert (
            write(
                client, "PATCH", path, {"description": "Replace this text"}, "before-description"
            ).status_code
            == 200
        )
        updated = write(client, "PATCH", path, {"description": description}, "description")
        assert updated.status_code == 200
        assert updated.json()["description"] == description
    markdown = "**Public result**: [docs](https://example.org/docs), #1, commit:abcdef1"
    comment = write(client, "POST", "/api/v1/repos/project/todos/1/comments", {"body": markdown})
    assert comment.status_code == 201
    page = client.get("/r/project/1")
    assert page.status_code == 200
    assert "<strong>Public result</strong>" in page.text
    assert 'href="https://example.org/docs"' in page.text
    assert "Fix authentication — safely" in page.text
    assert "Project &amp; tools" in page.text


def test_existing_records_remain_readable_and_unrelated_updates_work(client, db_conn):
    repo, todo = seed(client)
    legacy = prohibited("hidden infrastructure")
    # Trusted-store insertion models an existing database, not a supported HTTP write.
    store.update_repo(db_conn, repo["id"], {"navigation": [{"label": "docs", "url": legacy}]})
    store.update_todo(db_conn, todo["id"], {"description": legacy, "assignee": legacy})
    assert client.get("/api/v1/repos", headers=AUTH_HEADERS).status_code == 200
    assert client.get("/api/v1/repos/project/todos/1", headers=AUTH_HEADERS).status_code == 200
    assert client.get("/r/project/1").status_code == 200
    filtered = client.get(
        "/api/v1/repos/project/todos", headers=AUTH_HEADERS, params={"assignee": legacy}
    )
    assert filtered.status_code == 200
    assert len(filtered.json()["todos"]) == 1
    assert client.get("/r/project", params={"assignee": legacy}).status_code == 200
    assert write(client, "PATCH", "/api/v1/repos/project", {"name": "Renamed"}).status_code == 200
    assert (
        write(client, "PATCH", "/api/v1/repos/project/todos/1", {"priority": 2}).status_code == 200
    )


def test_repository_slug_keeps_grammar_and_public_policy(client, db_conn):
    for slug in ("synthetic" + ".internal", "..", "not/a/slug"):
        before = list(db_conn.iterdump())
        response = write(client, "POST", "/api/v1/repos", {"slug": slug, "name": "Project"})
        assert response.status_code == 422
        assert list(db_conn.iterdump()) == before


@pytest.mark.parametrize("field", ["source_repo", "target_repo", "source_ref"])
def test_git_closure_metadata_rejected_before_persistence(
    client, connection_factory, db_conn, field
):
    seed(client)
    headers = issue_token(connection_factory, "synthetic-closure-token", "git", ["git:close"])
    payload = {
        "source_repo": "project",
        "target_repo": "project",
        "source_ref": "refs/heads/master",
        "target_number": 1,
        "commit_sha": "a" * 40,
    }
    value = "synthetic" + ".internal"
    payload[field] = "refs/heads/" + value if field == "source_ref" else value
    # Authentication updates last_used_at neither before nor after this request.
    before = list(db_conn.iterdump())
    response = client.post(
        "/api/v1/git-closures", json=payload, headers={**headers, "Idempotency-Key": "closure"}
    )
    assert response.status_code == 422
    assert "hidden infrastructure" in response.text
    assert value not in response.text
    assert list(db_conn.iterdump()) == before


def directives(response) -> dict[str, list[str]]:
    return {
        parts[0]: parts[1:]
        for item in response.headers["content-security-policy"].split(";")
        if (parts := item.split())
    }


def assert_baseline(response):
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "no-referrer"
    policy = directives(response)
    for name in ("default-src", "object-src", "base-uri", "frame-ancestors"):
        assert policy[name] == ["'none'"]
    assert policy["form-action"] == ["'self'"]
    assert policy["connect-src"] == ["'self'"]
    assert "'unsafe-inline'" not in policy["script-src"]
    assert "'unsafe-eval'" not in policy["script-src"]
    assert "*" not in response.headers["content-security-policy"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    ("path", "status", "authenticated"),
    [
        ("/", 200, False),
        ("/health", 200, False),
        ("/missing", 404, False),
        ("/api/v1/repos", 401, False),
        ("/api/v1/repos", 200, True),
        ("/api/v1/repos/", 307, True),
        ("/api/v1/docs", 200, True),
        ("/api/v1/docs", 401, False),
        ("/api/v1/openapi.json", 200, True),
        ("/api/v1/repos/project/todos?state=invalid", 422, True),
        ("/static/tracker.css", 200, False),
        ("/static/filter-complete.js", 200, False),
        ("/static/chi-rho.svg", 200, False),
        ("/static/missing.css", 404, False),
    ],
)
def test_security_headers_across_response_paths(client, method, path, status, authenticated):
    response = client.request(
        method, path, headers=AUTH_HEADERS if authenticated else {}, follow_redirects=False
    )
    assert response.status_code == status
    assert_baseline(response)
    if method == "HEAD":
        assert response.content == b""


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_unhandled_failure_has_security_headers(settings, connection_factory, method):
    app = create_app(settings, get_conn=connection_factory)

    @app.get("/synthetic-failure")
    def failure():
        raise RuntimeError("synthetic failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(method, "/synthetic-failure")
    assert response.status_code == 500
    assert_baseline(response)
    assert "synthetic failure" not in response.text
    if method == "HEAD":
        assert response.content == b""


def test_mutation_and_method_failures_have_security_headers(client):
    for response in (
        write(client, "POST", "/api/v1/repos", {}),
        client.post("/health"),
        write(client, "POST", "/api/v1/git-closures", {}),
    ):
        assert response.status_code in (422, 405, 403)
        assert_baseline(response)


def csp_hash(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode() + "'"


def test_csp_allows_only_known_inline_styles_and_docs_script(client):
    seed(client)
    write(client, "PATCH", "/api/v1/repos/project/todos/1", {"labels": ["bug", "custom"]})
    for path in ("/r/project", "/r/project/1"):
        page = client.get(path)
        policy = directives(page)
        assert policy["script-src"] == ["'self'"]
        assert "'unsafe-inline'" not in page.headers["content-security-policy"]
        assert "'unsafe-hashes'" in policy["style-src-attr"]
        for style in re.findall(r'style="([^"]+)"', page.text):
            assert csp_hash(unescape(style)) in policy["style-src-attr"]
    svg = client.get("/static/chi-rho.svg")
    # Hash the browser-visible XML text, not the serialized CDATA delimiters.
    with minidom.parseString(svg.content) as document:
        styles = document.getElementsByTagNameNS("http://www.w3.org/2000/svg", "style")
        assert styles
        for style in styles:
            text = "".join(
                node.data
                for node in style.childNodes
                if node.nodeType in (node.TEXT_NODE, node.CDATA_SECTION_NODE)
            )
            assert csp_hash(text) in directives(svg)["style-src"]
    assert "'unsafe-inline'" not in svg.headers["content-security-policy"]
    docs = client.get("/api/v1/docs", headers=AUTH_HEADERS)
    policy = directives(docs)
    for source in re.findall(r"<script>(.*?)</script>", docs.text, re.S):
        assert csp_hash(source) in policy["script-src"]
    for source in re.findall(r'<script src="([^"]+)"', docs.text):
        assert source in policy["script-src"]
    for source in re.findall(r'<link type="text/css" rel="stylesheet" href="([^"]+)"', docs.text):
        assert source in policy["style-src"]
    assert '"validatorUrl": null' in docs.text
    assert_baseline(docs)


def test_static_not_modified_keeps_security_headers(client):
    asset = client.get("/static/tracker.css")
    cached = client.get("/static/tracker.css", headers={"If-None-Match": asset.headers["etag"]})
    assert cached.status_code == 304
    assert_baseline(cached)


def test_docs_hash_covers_root_path_initializer(settings, connection_factory):
    settings = Settings(
        db_path=settings.db_path,
        auth_token=settings.auth_token,
        cgit_base=settings.cgit_base,
        root_path="/todos",
    )
    with TestClient(
        create_app(settings, get_conn=connection_factory), root_path="/todos"
    ) as client:
        docs = client.get("/api/v1/docs", headers=AUTH_HEADERS)
        assert docs.status_code == 200
        assert "url: '/todos/api/v1/openapi.json'" in docs.text
        policy = directives(docs)
        for source in re.findall(r"<script>(.*?)</script>", docs.text, re.S):
            assert csp_hash(source) in policy["script-src"]
        assert_baseline(docs)
