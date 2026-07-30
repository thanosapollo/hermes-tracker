"""Tests for public read-only HTML frontend."""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from conftest import AUTH_HEADERS
from hermes_tracker.app import ASSET_VERSION, create_app
from hermes_tracker.config import Settings


def _seed_full_scenario(client) -> None:
    """Create a repo, todo with criteria, comment, and closing data."""
    client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
    client.post(
        "/api/v1/repos/proj/todos",
        json={
            "title": "Fix login bug",
            "description": "Users cannot log in with uppercase emails.",
            "priority": 5,
            "acceptance_criteria": ["Tests pass", "Manual smoke test"],
            "verification_commands": ["pytest -q", "ruff check src"],
        },
        headers={**AUTH_HEADERS, "X-Actor": "agent@example.org"},
    )
    client.post(
        "/api/v1/repos/proj/todos/1/comments",
        json={"author": "agent-1", "body": "Root cause: case-sensitive email comparison."},
        headers=AUTH_HEADERS,
    )


def _seed_markdown_scenario(client, *, description: str, comment: str) -> None:
    """Create public content whose presentation is rendered as Markdown."""
    client.post(
        "/api/v1/repos",
        json={"slug": "markdown", "name": "Markdown", "description": description},
        headers=AUTH_HEADERS,
    )
    client.post(
        "/api/v1/repos/markdown/todos",
        json={"title": "Render safely", "description": description},
        headers=AUTH_HEADERS,
    )
    client.post(
        "/api/v1/repos/markdown/todos/1/comments",
        json={"author": "agent", "body": comment},
        headers=AUTH_HEADERS,
    )


class TestPublicFrontend:
    def test_index_shows_repos(self, client) -> None:
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Project" in resp.text

    def test_unpublished_repo_and_history_are_absent_from_public_routes(self, client) -> None:
        client.post(
            "/api/v1/repos", json={"slug": "withdrawn", "name": "Withdrawn"}, headers=AUTH_HEADERS
        )
        client.post(
            "/api/v1/repos/withdrawn/todos",
            json={"title": "Retained history"},
            headers=AUTH_HEADERS,
        )
        response = client.patch(
            "/api/v1/repos/withdrawn", json={"published": False}, headers=AUTH_HEADERS
        )

        assert response.status_code == 200
        assert response.json()["published"] is False
        assert "Withdrawn" not in client.get("/").text
        assert client.get("/r/withdrawn").status_code == 404
        assert client.get("/r/withdrawn/1").status_code == 404

        republished = client.patch(
            "/api/v1/repos/withdrawn", json={"published": True}, headers=AUTH_HEADERS
        )
        assert republished.status_code == 200
        assert "Retained history" in client.get("/r/withdrawn").text

    def test_repo_detail_shows_todos(self, client) -> None:
        _seed_full_scenario(client)
        resp = client.get("/r/proj")
        assert resp.status_code == 200
        assert "Fix login bug" in resp.text
        assert "#1" in resp.text

    def test_repo_detail_defaults_open_and_filters(self, client) -> None:
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        client.post(
            "/api/v1/repos/proj/todos",
            json={
                "title": "Open Auth",
                "labels": ["auth", "bug"],
                "assignee": "alice",
            },
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Docs only", "labels": ["docs"], "assignee": "bob"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Closed Auth", "labels": ["auth"]},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos/3/close",
            json={"verification_output": "done"},
            headers=AUTH_HEADERS,
        )

        default = client.get("/r/proj")
        assert default.status_code == 200
        assert "Open Auth" in default.text
        assert "Docs only" in default.text
        assert "Closed Auth" not in default.text
        assert 'name="q"' in default.text
        assert 'name="state"' in default.text
        assert 'value="open"' in default.text

        filtered = client.get("/r/proj?q=auth&label=bug&assignee=alice&state=open")
        assert filtered.status_code == 200
        assert "Open Auth" in filtered.text
        assert "Docs only" not in filtered.text
        assert re.search(r'name="q"[^>]*value="auth"', filtered.text)
        assert re.search(r'name="label"[^>]*value="bug"', filtered.text)
        assert re.search(r'name="assignee"[^>]*value="alice"', filtered.text)

        closed = client.get("/r/proj?state=closed")
        assert "Closed Auth" in closed.text
        assert "Open Auth" not in closed.text

    def test_repo_detail_filter_suggestions_and_title_copy(self, client) -> None:
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Open Auth", "labels": ["auth", "bug"], "assignee": "alice"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Closed Docs", "labels": ["docs"], "assignee": "bob"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos/2/close",
            json={"verification_output": "done"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos",
            json={"slug": "other", "name": "Other"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/other/todos",
            json={"title": "Elsewhere", "labels": ["global"], "assignee": "carol"},
            headers=AUTH_HEADERS,
        )

        page = client.get("/r/proj")
        assert page.status_code == 200
        text = page.text
        assert 'placeholder="Search titles…"' in text or 'placeholder="Search titles..."' in text
        assert 'placeholder="substring"' not in text
        assert re.search(r'name="q"[^>]*list=', text) is None
        assert 'name="q"' in text
        assert 'list="label-suggestions"' in text
        assert 'list="assignee-suggestions"' in text
        assert 'id="label-suggestions"' in text
        assert 'id="assignee-suggestions"' in text
        assert '<option value="auth">' in text
        assert '<option value="bug">' in text
        assert '<option value="docs">' in text
        assert '<option value="alice">' in text
        assert '<option value="bob">' in text
        assert '<option value="global">' not in text
        assert '<option value="carol">' not in text
        assert "Open Auth" not in re.search(
            r'id="label-suggestions">(.*?)</datalist>', text, re.S
        ).group(1)
        assert "data-token-complete" in text
        assert re.search(
            r"filter-complete\.js\?v=[0-9a-f]{16}",
            text,
        )
        assert client.get("/static/filter-complete.js").status_code == 200

        other = client.get("/r/other").text
        assert '<option value="global">' in other
        assert '<option value="carol">' in other
        assert '<option value="auth">' not in other

    def test_todo_detail_shows_all_fields(self, client) -> None:
        _seed_full_scenario(client)
        resp = client.get("/r/proj/1")
        assert resp.status_code == 200
        text = resp.text
        assert "Fix login bug" in text
        assert "uppercase emails" in text
        assert "Tests pass" in text
        assert "pytest -q" in text
        assert "<th>Author</th><td>agent@example.org</td>" in text
        assert "agent-1" in text
        assert "case-sensitive" in text

    def test_todo_detail_renders_unknown_author_for_legacy_row(self, client, db_conn) -> None:
        client.post(
            "/api/v1/repos", json={"slug": "legacy", "name": "Legacy"}, headers=AUTH_HEADERS
        )
        repo_id = db_conn.execute("SELECT id FROM repositories WHERE slug = 'legacy'").fetchone()[0]
        db_conn.execute(
            """INSERT INTO todos
                 (repo_id, number, title, status, priority, acceptance_criteria,
                  verification_commands, created_at, updated_at)
               VALUES (?, 1, 'Imported', 'open', 0, '[]', '[]', 'old', 'old')""",
            (repo_id,),
        )
        db_conn.commit()

        text = client.get("/r/legacy/1").text

        assert "<th>Author</th><td>—</td>" in text

    def test_todo_detail_no_mutation_controls(self, client) -> None:
        _seed_full_scenario(client)
        resp = client.get("/r/proj/1")
        text = resp.text.lower()
        assert "<form" not in text
        assert "<button" not in text
        assert 'type="submit"' not in text
        assert "delete" not in text
        assert "edit" not in text

    def test_closure_card_precedes_comments_and_links_commit(self, client, db_conn) -> None:
        _seed_full_scenario(client)
        response = client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={
                "verification_output": "**Passed** for #1 and other#2",
                "closing_commit": "abc123",
            },
            headers={**AUTH_HEADERS, "X-Actor": "codex"},
        )
        assert response.status_code == 200

        configured_app = create_app(
            Settings(
                db_path=":memory:",
                auth_token="test-secret-token",
                cgit_base="https://git.thanosapollo.org",
            ),
            get_conn=lambda: db_conn,
        )
        with TestClient(configured_app) as configured_client:
            detail = configured_client.get("/r/proj/1").text

        assert "codex closed this:" in detail
        assert '<div class="comment closure">' in detail
        assert "<strong>Passed</strong>" in detail
        assert 'href="/r/proj/1">#1</a>' in detail
        assert 'href="/r/other/2">other#2</a>' in detail
        assert 'href="https://git.thanosapollo.org/proj/commit/?id=abc123"' in detail
        assert detail.index("comment closure") < detail.index("<h3>Comments</h3>")
        assert "Verification Output" not in detail
        assert "<th>Closing commit</th>" not in detail

    def test_actorless_closure_card_uses_generic_heading(self, client) -> None:
        _seed_full_scenario(client)
        client.post(
            "/api/v1/repos/proj/todos/1/close",
            json={"verification_output": "<script>bad()</script> safe"},
            headers=AUTH_HEADERS,
        )

        detail = client.get("/r/proj/1").text

        assert "<strong>Closed:</strong>" in detail
        assert "<script>bad()" not in detail
        assert "&lt;script&gt;" in detail

    def test_todo_references_linkify_only_eligible_prose(self, client) -> None:
        source = (
            "Local #10, cross repo-name#11, `#12`, and [#13](/already). "
            "Escaped \\#14, malformed #0 repo##15 word#16 #17x.\n\n"
            "```text\n#18\n```"
        )
        _seed_markdown_scenario(client, description=source, comment=source)

        detail = client.get("/r/markdown/1").text

        assert 'href="/r/markdown/10">#10</a>' in detail
        assert 'href="/r/repo-name/11">repo-name#11</a>' in detail
        assert "<code>#12</code>" in detail
        assert '<a href="/already">#13</a>' in detail
        assert '<a href="/r/markdown/14"' not in detail
        assert '<a href="/r/markdown/18"' not in detail
        for malformed in ("#0", "repo##15", "word#16", "#17x"):
            assert malformed in detail

    def test_commit_hashes_link_in_descriptions_comments_and_inline_code(self, db_conn) -> None:
        source = (
            "Fix a01e2c7 and 0123456789abcdef0123456789abcdef01234567; "
            "inline `deadbee`, existing [a01e2c7](https://example.com/a01e2c7), "
            "and fenced:\n\n```text\na01e2c7\n```"
        )
        application = create_app(
            Settings(
                db_path=":memory:",
                auth_token="test-secret-token",
                cgit_base="https://git.thanosapollo.org",
            ),
            get_conn=lambda: db_conn,
        )
        with TestClient(application) as configured_client:
            _seed_markdown_scenario(configured_client, description=source, comment=source)
            detail = configured_client.get("/r/markdown/1").text

        expected = 'href="https://git.thanosapollo.org/markdown/commit/?id=a01e2c7"'
        assert detail.count(expected) == 2
        assert '<code><a href="https://git.thanosapollo.org/markdown/commit/?id=deadbee"' in detail
        assert (
            'href="https://git.thanosapollo.org/markdown/commit/?id='
            '0123456789abcdef0123456789abcdef01234567"'
        ) in detail
        assert (
            '<a href="https://example.com/a01e2c7" rel="nofollow noopener noreferrer">a01e2c7</a>'
        ) in detail
        assert (
            '<a href="https://git.thanosapollo.org/markdown/commit/?id=a01e2c7">a01e2c7</a>'
            in detail
        )
        assert detail.count("a01e2c7") >= 4

    def test_commit_hash_exclusions_and_cgit_fallback(self, db_conn) -> None:
        source = (
            "embedded a01e2c7x, overlong "
            "0123456789abcdef0123456789abcdef012345678, and `not-a-hash`."
        )
        application = create_app(
            Settings(db_path=":memory:", auth_token="test-secret-token"),
            get_conn=lambda: db_conn,
        )
        with TestClient(application) as configured_client:
            _seed_markdown_scenario(configured_client, description=source, comment=source)
            detail = configured_client.get("/r/markdown/1").text

        assert 'href="/commit/' not in detail
        assert "a01e2c7x" in detail
        assert "0123456789abcdef0123456789abcdef012345678" in detail
        assert "not-a-hash" in detail

    def test_repository_descriptions_do_not_linkify_todo_references(self, client) -> None:
        _seed_markdown_scenario(client, description="See #10", comment="plain")

        repo_detail = client.get("/r/markdown").text

        assert 'href="/r/markdown/10"' not in repo_detail

    def test_public_markdown_renders_supported_tracker_syntax(self, client) -> None:
        markdown = """## Summary

**Bold** and *emphasis* with `inline()`.

- one
- two

> quoted

```python
print("https://example.com/not-a-link")
```

| Key | Value |
| --- | --- |
| state | open |

https://example.com/path
"""
        _seed_markdown_scenario(client, description=markdown, comment="**Comment**")

        detail = client.get("/r/markdown/1").text

        assert "<h5>Summary</h5>" in detail
        assert "<strong>Bold</strong>" in detail
        assert "<em>emphasis</em>" in detail
        assert "<code>inline()</code>" in detail
        assert "<ul>" in detail
        assert "<blockquote>" in detail
        assert '<code class="language-python">' in detail
        assert '<a href="https://example.com/not-a-link"' not in detail
        assert "<table>" in detail
        assert '<a href="https://example.com/path"' in detail
        assert "<strong>Comment</strong>" in detail

    def test_public_markdown_escapes_raw_html_and_unsafe_attributes(self, client) -> None:
        payload = '<script>alert(1)</script><b onclick="alert(2)">raw</b>'
        _seed_markdown_scenario(client, description=payload, comment=payload)

        detail = client.get("/r/markdown/1").text.lower()

        assert "<script>alert" not in detail
        assert '<b onclick="' not in detail
        assert "&lt;script&gt;" in detail
        assert "&lt;b" in detail

    def test_public_markdown_rejects_dangerous_protocols_and_nested_payloads(self, client) -> None:
        payload = (
            "[javascript](javascript:alert(1)) "
            "[data](data:text/html,<script>alert(2)</script>) "
            "[vbscript](vbscript:msgbox(3)) "
            "[encoded](jav&#x61;script:alert(4)) "
            "![image](data:image/svg+xml,<svg onload=alert(5)>) "
            "[safe](https://example.com)\n\n"
            "<svg><a xlink:href=javascript:alert(3)>nested</a></svg>"
        )
        _seed_markdown_scenario(client, description=payload, comment=payload)

        detail = client.get("/r/markdown/1").text.lower()
        content = detail.split('<main class="content">', 1)[1].split("</main>", 1)[0]

        assert 'href="javascript:' not in detail
        assert 'href="data:' not in detail
        assert 'href="vbscript:' not in detail
        assert 'src="data:' not in detail
        assert "<img" not in content
        assert "<svg" not in content
        assert "<a xlink:href" not in content
        assert 'href="https://example.com"' in detail

    def test_external_markdown_links_have_safe_rel_attributes(self, client) -> None:
        _seed_markdown_scenario(
            client,
            description="[external](https://example.com) and [local](/r/markdown)",
            comment="https://example.org",
        )

        detail = client.get("/r/markdown/1").text

        assert 'href="https://example.com" rel="nofollow noopener noreferrer"' in detail
        assert 'href="https://example.org" rel="nofollow noopener noreferrer"' in detail
        assert 'href="/r/markdown"' in detail

    def test_markdown_code_blocks_do_not_double_escape_entities(self, client) -> None:
        """Entities like > and & inside code blocks must render once, not twice."""
        _seed_markdown_scenario(
            client,
            description="```python\nif a > b and c < d:\n    print('tom & jerry')\n```",
            comment="inline `a > b` code",
        )

        detail = client.get("/r/markdown/1").text

        # Single-escaped in code block
        assert "if a &gt; b and c &lt; d:" in detail
        assert "&amp;gt;" not in detail
        assert "&amp;lt;" not in detail
        assert "tom &amp; jerry" in detail
        assert "&amp;amp;" not in detail
        # Single-escaped in inline code
        assert "<code>a &gt; b</code>" in detail

    def test_markdown_headings_are_downranked_from_page_hierarchy(self, client) -> None:
        """User-authored h1-h3 must shift to h4-h6 to not clash with page headings."""
        _seed_markdown_scenario(
            client,
            description="# Big\n## Mid\n### Small\n#### Keep\n##### Keep\n###### Keep",
            comment="**bold**",
        )

        detail = client.get("/r/markdown/1").text

        assert "<h4>Big</h4>" in detail
        assert "<h5>Mid</h5>" in detail
        assert "<h6>Small</h6>" in detail
        assert "<h1>Big</h1>" not in detail
        assert "<h2>Mid</h2>" not in detail
        assert "<h3>Small</h3>" not in detail
        # h4-h6 stay as-is
        assert "<h4>Keep</h4>" in detail
        assert "<h5>Keep</h5>" in detail
        assert "<h6>Keep</h6>" in detail

    def test_markdown_source_remains_plain_text_in_api(self, client) -> None:
        source = "**stored source**"
        _seed_markdown_scenario(client, description=source, comment=source)

        todo = client.get("/api/v1/repos/markdown/todos/1", headers=AUTH_HEADERS).json()
        comments = client.get(
            "/api/v1/repos/markdown/todos/1/comments", headers=AUTH_HEADERS
        ).json()

        assert todo["description"] == source
        assert comments[0]["body"] == source

    def test_page_uses_one_external_custom_stylesheet_without_theme_runtime(self, client) -> None:
        html = client.get("/").text

        assert html.count('rel="stylesheet"') == 1
        assert re.search(r'href="http://testserver/static/tracker\.css\?v=[0-9a-f]{16}"', html)
        assert "<style" not in html
        assert "<script" not in html
        assert "Appearance" not in html
        assert "theme-select" not in html
        assert "localStorage" not in html
        assert "matchMedia" not in html
        assert "data-theme" not in html

    def test_page_stylesheet_url_matches_packaged_asset_version(self, client) -> None:
        html = client.get("/").text
        match = re.search(r"/static/tracker\.css\?v=([0-9a-f]{16})", html)

        assert match is not None
        assert match.group(1) == ASSET_VERSION
        stylesheet = client.get(f"/static/tracker.css?v={match.group(1)}")
        assert stylesheet.status_code == 200

    def test_static_stylesheet_has_cache_and_css_content_type(self, client) -> None:
        response = client.get("/static/tracker.css")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")
        assert response.headers["cache-control"] == "public, max-age=3600"
        assert "--bg: #000000" in response.text
        assert "--link: #d48a20" in response.text
        assert "@media (max-width: 600px)" in response.text
        assert response.text.count("max-width: 90ch") == 2

    def test_header_uses_local_chi_rho_and_current_branding(self, client) -> None:
        html = client.get("/").text

        assert 'src="http://testserver/static/chi-rho.svg"' in html
        assert "git.thanosapollo.org/chi-rho.svg" not in html
        assert 'alt="Chi-Rho"' in html
        assert "Hermes Tracker" in html
        assert "Agentum" not in html

        logo = client.get("/static/chi-rho.svg")
        assert logo.status_code == 200
        assert logo.headers["content-type"].startswith("image/svg+xml")

    def test_repository_prose_is_limited_without_centering_todos_or_comments(self, client) -> None:
        css = client.get("/static/tracker.css").text

        assert "max-width: 90ch" in css
        assert "margin-inline: auto" in css
        assert ".todo-description," in css
        assert ".comment-body" in css
        assert css.count("max-width: 90ch") == 2
        assert "margin-inline: 0" in css

        _seed_markdown_scenario(client, description="TODO prose", comment="Comment prose")
        detail = client.get("/r/markdown/1").text
        assert 'class="markdown todo-description"' in detail
        assert 'class="markdown comment-body"' in detail

    def test_index_uses_canonical_cgit_dark_tokens(self, client) -> None:
        """The stylesheet must faithfully match the canonical cgit dark palette."""
        css = client.get("/static/tracker.css").text

        # Core cgit dark tokens (exact values from cgit-dark-v2.css)
        assert "--bg: #000000" in css
        assert "--panel: #0a0a0a" in css
        assert "--text: #d4c5a0" in css
        assert "--text-muted: #9a8e78" in css
        assert "--border: #201a14" in css
        assert "--link: #d48a20" in css
        assert "--link-hover: #e89e30" in css
        assert "--code-bg: #0f0e0c" in css
        # Monospace font stack matching cgit
        assert "iosevka" in css
        assert "monospace" in css
        # Semantic status tokens with cgit-derived accent colors
        assert "--status-open:" in css
        assert "--status-active:" in css
        assert "--status-blocked:" in css
        assert "--status-done:" in css

    def test_status_pills_use_semantic_tokens_not_literal_colors(self, client) -> None:
        _seed_full_scenario(client)
        html = client.get("/r/proj").text

        assert "--status-color: var(--status-open)" in html
        assert 'style="background:' not in html

    def test_theme_exposes_shared_semantic_accent_palette(self, client) -> None:
        """The shared --accent-* palette must remain in the canonical stylesheet."""
        css = client.get("/static/tracker.css").text

        for token in (
            "--accent-gold",
            "--accent-purple",
            "--accent-aegean",
            "--accent-teal",
            "--accent-olive",
            "--accent-terracotta",
        ):
            assert token in css

        # Dark palette (exact task values)
        assert "--accent-gold: #d48a20" in css
        assert "--accent-purple: #9b72cf" in css
        assert "--accent-aegean: #5b9bd5" in css
        assert "--accent-teal: #4fa89b" in css
        assert "--accent-olive: #8fa35b" in css
        assert "--accent-terracotta: #c76d52" in css

        # Status tokens must be aliases, not literal colors
        assert "--status-open: var(--accent-aegean)" in css
        assert "--status-active: var(--accent-teal)" in css
        assert "--status-blocked: var(--accent-terracotta)" in css
        assert "--status-done: var(--accent-olive)" in css
        assert "--status-other: var(--accent-purple)" in css

    def test_unknown_repo_404(self, client) -> None:
        resp = client.get("/r/nope")
        assert resp.status_code == 404

    def test_unknown_todo_404(self, client) -> None:
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        resp = client.get("/r/proj/999")
        assert resp.status_code == 404

    def test_empty_repo_list(self, client) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "No repositories" in resp.text

    def test_repo_list_has_reciprocal_cgit_navigation(self, db_conn) -> None:
        application = create_app(
            settings=Settings(
                db_path=":memory:",
                auth_token="test-secret-token",
                cgit_base="https://git.thanosapollo.org",
            ),
            get_conn=lambda: db_conn,
        )
        with TestClient(application) as client:
            html = client.get("/").text

        assert 'aria-label="Site navigation"' in html
        assert '<a href="https://git.thanosapollo.org">index</a>' in html
        assert 'href="http://testserver/" class="active">todos</a>' in html

    def test_public_endpoints_no_auth_required(self, client) -> None:
        """The public HTML must work with no Authorization header at all."""
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        resp = client.get("/")
        assert resp.status_code == 200


class TestCgitParity:
    """Focused tests for cgit visual/structural parity."""

    def test_index_is_compact_table_not_cards(self, client) -> None:
        """The repo index must be a table.list, not card/tile/dashboard markup."""
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "description": "A project"},
            headers=AUTH_HEADERS,
        )
        html = client.get("/").text

        assert 'class="list"' in html
        assert 'aria-label="Repository list"' in html
        assert ">NAME<" in html or ">NAME " in html or "NAME</th>" in html
        assert "DESCRIPTION" in html
        # No card/tile/dashboard classes
        assert 'class="card"' not in html
        assert "tile" not in html.lower()
        assert "dashboard" not in html.lower()

    def test_index_shows_open_count_and_updated_metadata(self, client) -> None:
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "description": "Test"},
            headers=AUTH_HEADERS,
        )
        client.post(
            "/api/v1/repos/proj/todos",
            json={"title": "Task one"},
            headers=AUTH_HEADERS,
        )
        html = client.get("/").text

        assert "OPEN" in html
        assert "UPDATED" in html
        # The open count should be rendered (1 open todo)
        assert "meta-open" in html

    def test_no_card_classes_in_any_view(self, client) -> None:
        """Card presentation must be removed from all views."""
        _seed_full_scenario(client)
        for path in ("/", "/r/proj", "/r/proj/1"):
            html = client.get(path).text
            assert 'class="card"' not in html
            assert "box-shadow" not in html.replace("-shadow", "")
            assert "shadow" not in html or "--shadow: none" in html

    def test_cgit_header_proportions_and_full_width_panel(self, client) -> None:
        """Header must be a full-width panel with border-bottom, not a max-width bar."""
        html = client.get("/").text
        css = client.get("/static/tracker.css").text

        assert 'class="header"' in html or 'class="header' in html
        assert "--panel:" in css
        # No max-width centering on body (cgit is full-width)
        assert "max-width: 920px" not in css
        assert "max-width: 800px" not in css

    def test_repo_detail_has_cgit_tabs_with_todos_active(self, client) -> None:
        """Repository page must show cgit-style tabs with 'todos' as active."""
        _seed_full_scenario(client)
        html = client.get("/r/proj").text

        assert 'class="tabs"' in html or 'class="tabs ' in html
        assert "todos" in html
        # The todos tab links to the current page and is active
        assert 'class="active"' in html

    def test_active_tab_uses_cgit_teal_accent(self, client) -> None:
        """The active tab text and underline must match cgit's teal accent."""
        css = client.get("/static/tracker.css").text

        # Isolate the .tabs a.active rule body.
        rule_start = css.index(".tabs a.active {")
        rule_body = css[rule_start : css.index("}", rule_start) + 1]

        assert "color: var(--accent-teal)" in rule_body
        assert "border-bottom-color: var(--accent-teal)" in rule_body
        # No longer using the generic gold/text accents for the active tab.
        assert "var(--text)" not in rule_body
        assert "var(--link)" not in rule_body

    def test_mobile_repository_list_hides_description_column(self, client) -> None:
        """At ≤600px the repo-index DESCRIPTION column must hide; other tables untouched."""
        css = client.get("/static/tracker.css").text

        # Seed a repository so the Repository list table actually renders.
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "description": "A project"},
            headers=AUTH_HEADERS,
        )
        html = client.get("/").text

        # The repository list table carries the accessible label used for scoping.
        assert 'aria-label="Repository list"' in html
        assert 'aria-label="Repository list"' in css

        media_start = css.index("@media (max-width: 600px)")
        media_body = css[media_start:]

        # DESCRIPTION column hidden on the repository index only.
        assert 'table.list[aria-label="Repository list"]' in media_body
        assert "td.description" in media_body
        assert "display: none" in media_body

        # The TODO list table is a different accessible label and must not be hidden.
        client.post("/api/v1/repos/proj/todos", json={"title": "Task"}, headers=AUTH_HEADERS)
        todo_detail_html = client.get("/r/proj").text
        assert 'aria-label="TODO list"' in todo_detail_html

    def test_repository_and_todo_render_exact_manifest_navigation_order(self, client) -> None:
        labels = ["about", "summary", "refs", "log", "tree", "commit", "diff", "homepage", "todos"]
        navigation = [
            {"label": label, "url": f"https://git.thanosapollo.org/proj/{label}"}
            for label in labels[:-1]
        ] + [{"label": "todos", "url": "/r/proj"}]
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "navigation": navigation},
            headers=AUTH_HEADERS,
        )
        client.post("/api/v1/repos/proj/todos", json={"title": "Task"}, headers=AUTH_HEADERS)

        for path in ("/r/proj", "/r/proj/1"):
            html = client.get(path).text
            positions = [html.index(f">{label}</a>") for label in labels]
            assert positions == sorted(positions)
            assert '<a href="http://testserver/r/proj" class="active">todos</a>' in html

    def test_empty_navigation_uses_legacy_cgit_fallback_on_repository_and_todo(
        self, db_conn
    ) -> None:
        application = create_app(
            settings=Settings(
                db_path=":memory:",
                auth_token="test-secret-token",
                cgit_base="https://git.thanosapollo.org",
            ),
            get_conn=lambda: db_conn,
        )
        with TestClient(application) as client:
            client.post(
                "/api/v1/repos",
                json={"slug": "project", "name": "Project"},
                headers=AUTH_HEADERS,
            )
            client.post(
                "/api/v1/repos/project/todos",
                json={"title": "Task"},
                headers=AUTH_HEADERS,
            )

            for path in ("/r/project", "/r/project/1"):
                html = client.get(path).text
                expected = [
                    'href="https://git.thanosapollo.org/project"',
                    'href="https://git.thanosapollo.org/project/log"',
                    'href="https://git.thanosapollo.org/project/tree"',
                    'href="http://testserver/r/project" class="active"',
                ]
                positions = [html.index(item) for item in expected]
                assert positions == sorted(positions)
                assert html.count(">todos</a>") == 1

    def test_declared_navigation_appends_canonical_todos_on_repository_and_todo(
        self, client
    ) -> None:
        navigation = [
            {"label": "tree", "url": "https://git.thanosapollo.org/proj/tree?view=a&sort=b"},
            {"label": "summary", "url": "https://git.thanosapollo.org/proj"},
        ]
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "navigation": navigation},
            headers=AUTH_HEADERS,
        )
        client.post("/api/v1/repos/proj/todos", json={"title": "Task"}, headers=AUTH_HEADERS)

        for path in ("/r/proj", "/r/proj/1"):
            html = client.get(path).text
            assert html.index(">tree</a>") < html.index(">summary</a>") < html.index(">todos</a>")
            assert 'href="https://git.thanosapollo.org/proj/tree?view=a&amp;sort=b"' in html
            assert '<a href="http://testserver/r/proj" class="active">todos</a>' in html
            assert html.count(">todos</a>") == 1

    def test_declared_todos_is_replaced_in_place_on_repository_and_todo(self, client) -> None:
        navigation = [
            {"label": "summary", "url": "https://git.thanosapollo.org/proj"},
            {"label": "todos", "url": "https://git.thanosapollo.org/wrong"},
            {"label": "tree", "url": "https://git.thanosapollo.org/proj/tree"},
        ]
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "navigation": navigation},
            headers=AUTH_HEADERS,
        )
        client.post("/api/v1/repos/proj/todos", json={"title": "Task"}, headers=AUTH_HEADERS)

        for path in ("/r/proj", "/r/proj/1"):
            html = client.get(path).text
            assert html.index(">summary</a>") < html.index(">todos</a>") < html.index(">tree</a>")
            assert '<a href="http://testserver/r/proj" class="active">todos</a>' in html
            assert "https://git.thanosapollo.org/wrong" not in html

    def test_duplicate_persisted_todos_are_collapsed_without_reordering_custom_entries(
        self, client, db_conn
    ) -> None:
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project"},
            headers=AUTH_HEADERS,
        )
        duplicate_navigation = [
            {"label": "summary", "url": "https://git.thanosapollo.org/proj"},
            {"label": "todos", "url": "/wrong-first"},
            {"label": "tree", "url": "https://git.thanosapollo.org/proj/tree"},
            {"label": "todos", "url": "/wrong-second"},
        ]
        db_conn.execute(
            "UPDATE repositories SET navigation = ? WHERE slug = ?",
            (json.dumps(duplicate_navigation), "proj"),
        )
        db_conn.commit()
        client.post("/api/v1/repos/proj/todos", json={"title": "Task"}, headers=AUTH_HEADERS)

        for path in ("/r/proj", "/r/proj/1"):
            html = client.get(path).text
            assert html.index(">summary</a>") < html.index(">todos</a>") < html.index(">tree</a>")
            assert html.count(">todos</a>") == 1
            assert '<a href="http://testserver/r/proj" class="active">todos</a>' in html

    def test_repository_navigation_preserves_custom_order_and_omits_homepage(self, client) -> None:
        navigation = [
            {"label": "tree", "url": "https://git.thanosapollo.org/proj/tree"},
            {"label": "summary", "url": "https://git.thanosapollo.org/proj"},
            {"label": "todos", "url": "/r/proj"},
        ]
        client.post(
            "/api/v1/repos",
            json={"slug": "proj", "name": "Project", "navigation": navigation},
            headers=AUTH_HEADERS,
        )
        html = client.get("/r/proj").text
        assert html.index(">tree</a>") < html.index(">summary</a>") < html.index(">todos</a>")
        assert ">homepage</a>" not in html

    def test_repository_without_navigation_renders_safe_minimal_fallback(self, client) -> None:
        client.post("/api/v1/repos", json={"slug": "proj", "name": "Project"}, headers=AUTH_HEADERS)
        html = client.get("/r/proj").text
        assert '<a href="http://testserver/r/proj" class="active">todos</a>' in html
        assert ">summary</a>" not in html

    def test_repo_detail_cgit_links_omitted_when_not_configured(self, client) -> None:
        """When cgit_base is not set, summary/log/tree links must be absent."""
        _seed_full_scenario(client)
        html = client.get("/r/proj").text

        # No cgit navigation links when base URL not configured
        assert ">summary<" not in html
        assert ">log<" not in html
        assert ">tree<" not in html

    def test_todo_detail_cgit_links_omitted_when_not_configured(self, client) -> None:
        """Todo detail also omits cgit links when base URL not configured."""
        _seed_full_scenario(client)
        html = client.get("/r/proj/1").text

        assert ">summary<" not in html
        assert ">log<" not in html
        assert ">tree<" not in html

    def test_repo_list_has_accessible_aria_labels(self, client) -> None:
        """Tables must have aria-labels for screen readers."""
        client.post("/api/v1/repos", json={"slug": "p", "name": "P"}, headers=AUTH_HEADERS)
        html = client.get("/").text

        assert 'aria-label="Repository list"' in html

    def test_repo_detail_has_breadcrumb_navigation(self, client) -> None:
        """Repository pages show cgit-style breadcrumb path."""
        _seed_full_scenario(client)
        html = client.get("/r/proj").text

        assert "breadcrumb" in html
        assert "index" in html
        assert "proj" in html

    def test_repo_detail_todo_table_is_compact_list(self, client) -> None:
        """Todo list on repo page must use table.list, not cards."""
        _seed_full_scenario(client)
        html = client.get("/r/proj").text

        assert 'aria-label="TODO list"' in html
        assert 'class="list"' in html

    def test_no_marketing_spacing_or_large_panels(self, client) -> None:
        """Verify no large rounded panels, excessive padding, or marketing spacing."""
        html = client.get("/").text

        # No large border radius on panels
        assert "border-radius: 16px" not in html
        assert "border-radius: 20px" not in html
        # No excessive padding on body
        assert "padding: 3rem" not in html
        assert "padding: 2.5rem" not in html

    def test_page_has_no_viewport_meta(self, client) -> None:
        """Tracker must omit the responsive viewport meta so mobile uses cgit's
        fixed 980px layout viewport."""
        html = client.get("/").text

        assert 'name="viewport"' not in html
        assert "width=device-width" not in html

    def test_header_has_cgit_two_row_structure(self, client) -> None:
        """Header must expose separate logo, main-title, and subtitle elements
        arranged in cgit's two-row grid geometry."""
        html = client.get("/").text
        css = client.get("/static/tracker.css").text

        # Separate semantic elements for logo, title, and subtitle.
        assert 'class="logo"' in html
        assert 'class="main"' in html
        assert 'class="sub"' in html
        assert "Hermes Tracker" in html
        assert 'alt="Chi-Rho"' in html

        # The header is a CSS grid with a 68px logo column (cgit geometry).
        header_rule = css[css.index(".header {") : css.index("}", css.index(".header {")) + 1]
        assert "display: grid" in header_rule
        assert "68px" in header_rule

        # Logo image is 40px (cgit metric), not the prior 42px.
        logo_img_rule = css[
            css.index(".header .logo img {") : css.index("}", css.index(".header .logo img {")) + 1
        ]
        assert "40px" in logo_img_rule

    def test_description_paragraphs_have_zero_margin_in_list(self, client) -> None:
        """Markdown <p> inside repository description cells must have zero margin
        so ordinary rows stay cgit-dense (37px) rather than inflating to 67px."""
        css = client.get("/static/tracker.css").text

        rule_start = css.index("table.list td.description")
        rule_end = css.index("}", rule_start) + 1
        # Search the region around the description rules for the p margin reset.
        region = css[rule_start : rule_end + 200]
        assert "td.description p" in region
        assert "margin: 0" in region


class TestCgitUrlDerivation:
    """Tests for cgit navigation URL derivation and fallback."""

    def test_valid_cgit_base_produces_navigation_links(self) -> None:
        from hermes_tracker.web.routes import _cgit_urls, _safe_cgit_base

        base = _safe_cgit_base("https://git.example.org")
        assert base == "https://git.example.org"
        urls = _cgit_urls(base, "emacs-jabber")
        assert urls["summary"] == "https://git.example.org/emacs-jabber"
        assert urls["log"] == "https://git.example.org/emacs-jabber/log"
        assert urls["tree"] == "https://git.example.org/emacs-jabber/tree"

    def test_empty_cgit_base_omits_all_links(self) -> None:
        from hermes_tracker.web.routes import _cgit_urls

        urls = _cgit_urls(None, "some-repo")
        assert urls["summary"] is None
        assert urls["log"] is None
        assert urls["tree"] is None

        urls = _cgit_urls("", "some-repo")
        assert urls["summary"] is None

    def test_invalid_cgit_base_rejected(self) -> None:
        from hermes_tracker.web.routes import _safe_cgit_base

        # http (not https) rejected
        assert _safe_cgit_base("http://git.example.org") is None
        # Credentials rejected
        assert _safe_cgit_base("https://user:password@git.example.org") is None
        # Query/fragment rejected
        assert _safe_cgit_base("https://git.example.org?x=1") is None
        assert _safe_cgit_base("https://git.example.org#frag") is None
        assert _safe_cgit_base("https://git.example.org:99999") is None
        # Empty
        assert _safe_cgit_base("") is None
        assert _safe_cgit_base("not-a-url") is None

    def test_cgit_urls_quote_slug_safely(self) -> None:
        from hermes_tracker.web.routes import _cgit_urls, _safe_cgit_base

        base = _safe_cgit_base("https://git.example.org")
        urls = _cgit_urls(base, "repo with spaces")
        summary = urls["summary"]
        assert summary is not None
        # Spaces must be encoded, not passed through
        assert " " not in summary
        assert "%20" in summary
