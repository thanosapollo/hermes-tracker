"""Public Markdown references are explicit navigation, never Git validation."""

from html.parser import HTMLParser

import pytest

from hermes_tracker.web.markdown import render_markdown

BASE = "https://git.example.org/cgit"


class ParsedHTML(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.anchors: list[dict[str, str | None]] = []
        self.stack: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "a":
            assert "a" not in self.stack, "nested anchor"
            self.anchors.append(attributes)
        if tag not in {"br", "hr", "img", "input", "meta", "link"}:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            del self.stack[self.stack.index(tag) :]


def rendered(source: str) -> str:
    return str(render_markdown(source, "example", BASE))


@pytest.mark.parametrize("kind", ["commit", "tree"])
@pytest.mark.parametrize("hash_value", ["AbC1234", "a" * 40, "a" * 41, "B" * 64])
def test_typed_git_reference_preserves_type_and_exact_hash(kind: str, hash_value: str) -> None:
    source = f"{kind}:{hash_value}"
    html = rendered(source)
    if kind == "tree":
        assert source in html
        assert ParsedHTML(html).anchors == []
        return
    assert f">{source}</a>" in html
    assert ParsedHTML(html).anchors == [
        {
            "href": f"{BASE}/example/{kind}/?id={hash_value}",
            "rel": "nofollow noopener noreferrer",
        }
    ]


@pytest.mark.parametrize(
    "source",
    [
        "abcdef0 20260915 " + "a" * 40 + " " + "b" * 64,
        "commit:abc123 tree:abc123",
        "commit:" + "a" * 65,
        "tree:" + "b" * 65,
        "commit:abcdefg tree:123456z",
        "Commit:abcdef0 TREE:abcdef0",
        "commit: abcdef0 tree:0xabcdef0",
        "precommit:abcdef0 some_tree:abcdef0",
        "commit:abcdef0suffix tree:abcdef0_foo",
        "commit:abcdef0/path tree:abcdef0-more",
        "commit:abcdef0~1 tree:abcdef0^2",
        "commit:abcdef0.foo tree:abcdef0?query",
        "commit:abcdef0=value tree:abcdef0%20",
        "commit:abcdef0+suffix tree:abcdef0@other",
        "commit:abcdef0\\path tree:abcdef0&other",
        "x/commit:abcdef0 x:tree:abcdef0",
        "écommit:abcdef0 tree:abcdef0é",
        "commit&#58;abcdef0 tree&#58;abcdef0",
    ],
)
def test_bare_and_malformed_references_are_not_guessed(source: str) -> None:
    assert ParsedHTML(rendered(source)).anchors == []


@pytest.mark.parametrize(
    "source, literal",
    [
        (r"\commit:AbC1234 \tree:AbC1234", "commit:AbC1234 tree:AbC1234"),
        ("`commit:AbC1234 tree:AbC1234`", "commit:AbC1234 tree:AbC1234"),
        (r"`\commit:AbC1234`", r"\commit:AbC1234"),
        ("```text\n\\tree:AbC1234\n```", r"\tree:AbC1234"),
        ("    commit:AbC1234\n", "commit:AbC1234"),
        (r"commit\:AbC1234", "commit:AbC1234"),
    ],
)
def test_literal_and_escaped_references_stay_literal(source: str, literal: str) -> None:
    html = rendered(source)
    assert literal in html
    assert ParsedHTML(html).anchors == []


@pytest.mark.parametrize(
    "source",
    [
        "[commit:AbC1234](https://example.org/tree/?id=AbC1234)",
        "[**tree:AbC1234**](https://example.org/tree/?id=AbC1234)",
        "[tree:AbC1234][chosen]\n\n[chosen]: https://example.org/tree/?id=AbC1234",
        "https://example.org/tree/?id=AbC1234",
    ],
)
def test_authored_urls_and_link_labels_are_not_rewritten(source: str) -> None:
    assert ParsedHTML(rendered(source)).anchors == [
        {
            "href": "https://example.org/tree/?id=AbC1234",
            "rel": "nofollow noopener noreferrer",
        }
    ]


def test_prose_boundaries_and_markdown_formatting() -> None:
    html = rendered("(commit:abcdef0), **tree:123abcd**.\n\n> commit:123abcd!")
    assert [a["href"] for a in ParsedHTML(html).anchors] == [
        f"{BASE}/example/commit/?id=abcdef0",
        f"{BASE}/example/commit/?id=123abcd",
    ]
    assert "<strong>" in html
    assert "<blockquote>" in html


def test_sanitization_still_applies_to_all_git_links() -> None:
    html = rendered(
        "Text <script>alert(1)</script> <img src=x onerror=alert(1)> "
        "[tree:abcdef0](javascript:alert%281%29) "
        'commit:abcdef0" onclick="alert(1) '
        '<a href="javascript:alert(1)">tree:abcdef0</a>'
    )
    parsed = ParsedHTML(html)
    assert all(tag not in {"script", "img"} for tag, _ in parsed.elements)
    assert all(not key.startswith("on") for _, attrs in parsed.elements for key in attrs)
    assert all(not (a.get("href") or "").startswith("javascript:") for a in parsed.anchors)
    assert sum(a.get("href") == f"{BASE}/example/commit/?id=abcdef0" for a in parsed.anchors) == 1


def test_renderer_escapes_configured_git_url_and_repository() -> None:
    html = str(render_markdown("commit:abcdef0", 'repo"/><script>', 'https://git.example/"'))
    parsed = ParsedHTML(html)
    assert len(parsed.anchors) == 1
    assert set(parsed.anchors[0]) == {"href", "rel"}
    assert all(tag != "script" for tag, _ in parsed.elements)


def test_context_is_per_render_and_missing_configuration_is_inert() -> None:
    assert ParsedHTML(str(render_markdown("commit:abcdef0", "one", BASE))).anchors
    assert not ParsedHTML(str(render_markdown("commit:abcdef0", "two"))).anchors
    assert not ParsedHTML(str(render_markdown("tree:abcdef0", cgit_base=BASE))).anchors
    assert "/two/commit/" in str(render_markdown("commit:abcdef0", "two", BASE))


def test_existing_todo_links_and_markdown_remain_unchanged() -> None:
    html = rendered(r"#12 other#3 \#4 `#5` **bold** [chosen](https://example.org) abcdef0")
    assert [a["href"] for a in ParsedHTML(html).anchors] == [
        "/r/example/12",
        "/r/other/3",
        "https://example.org",
    ]
    assert "#4 <code>#5</code> <strong>bold</strong>" in html
