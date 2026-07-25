"""Safe Markdown rendering for the public presentation layer."""

from __future__ import annotations

import re
import urllib.parse

import bleach  # type: ignore[import-untyped]
import mistune
from markupsafe import Markup

_ALLOWED_TAGS = frozenset(
    {
        "a",
        "blockquote",
        "br",
        "code",
        "del",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "a": ["href", "title", "rel"],
    "code": ["class"],
    "th": ["align"],
    "td": ["align"],
}
_ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto"})
_EXTERNAL_REL = "nofollow noopener noreferrer"

_MARKDOWN = mistune.create_markdown(
    escape=True,
    plugins=["strikethrough", "table", "url"],
)

_CLEANER = bleach.Cleaner(
    tags=_ALLOWED_TAGS,
    attributes=_ALLOWED_ATTRIBUTES,
    protocols=_ALLOWED_PROTOCOLS,
    strip=True,
    strip_comments=True,
)

# Matches <a href="http(s)://..."> that do not already carry a rel attribute so
# we can attach defensive relationship attributes *after* sanitization without
# re-escaping already-escaped entities (which bleach.linkify would do).
_EXTERNAL_LINK_RE = re.compile(r'<a href="(https?://[^"]+)"(?! rel=)')
_TODO_REFERENCE_RE = re.compile(
    r"(?<![\w./#-])(?:(?P<repo>[A-Za-z0-9_.-]+)#|#)(?P<number>[1-9]\d*)(?![\w#])"
)
_COMMIT_HASH_RE = re.compile(r"(?<![\w])(?P<hash>[0-9a-fA-F]{7,40})(?![\w])")
_HTML_PART_RE = re.compile(r"(<[^>]+>)")
_ESCAPED_REFERENCE_RE = re.compile(r"\\(?P<reference>(?:[A-Za-z0-9_.-]+)?#[1-9]\d*)(?![\w#])")


def _add_external_rel(html: str) -> str:
    """Append ``rel="nofollow noopener noreferrer"`` to external HTTP(S) links."""
    return _EXTERNAL_LINK_RE.sub(rf'<a href="\1" rel="{_EXTERNAL_REL}"', html)


# Shift h1-h3 from user-authored Markdown down to h4-h6 so embedded Markdown
# headings never compete with the page's semantic heading hierarchy (h1 site
# title, h2 page/context heading, h3 card or section heading).
_HEADING_RE = re.compile(r"<(/?)h([123])", re.IGNORECASE)


def _normalize_headings(html: str) -> str:
    """Down-rank user-authored headings from h1-h3 to h4-h6."""
    return _HEADING_RE.sub(lambda m: f"<{m.group(1)}h{int(m.group(2)) + 3}", html)


def _protect_escaped_references(source: str) -> tuple[str, str, list[str]]:
    protected: list[str] = []
    prefix = "HERMESTODOESCAPEDTOKEN"
    while prefix in source:
        prefix += "X"

    def replace(match: re.Match[str]) -> str:
        protected.append(match.group("reference"))
        return f"{prefix}{len(protected) - 1}END"

    return _ESCAPED_REFERENCE_RE.sub(replace, source), prefix, protected


def _linkify_references(html: str, repo_slug: str) -> str:
    parts = _HTML_PART_RE.split(html)
    excluded: list[str] = []
    rendered: list[str] = []
    for part in parts:
        tag = re.match(r"</?([A-Za-z0-9]+)", part)
        if tag:
            name = tag.group(1).lower()
            if part.startswith("</"):
                if excluded and excluded[-1] == name:
                    excluded.pop()
            elif name in {"a", "code", "pre"} and not part.endswith("/>"):
                excluded.append(name)
            rendered.append(part)
            continue
        if excluded:
            rendered.append(part)
            continue

        def replace(match: re.Match[str]) -> str:
            target_repo = match.group("repo") or repo_slug
            quoted_repo = urllib.parse.quote(target_repo, safe="")
            reference = match.group(0)
            return f'<a href="/r/{quoted_repo}/{match.group("number")}">{reference}</a>'

        rendered.append(_TODO_REFERENCE_RE.sub(replace, part))
    return "".join(rendered)


def _linkify_commit_hashes(html: str, repo_slug: str, cgit_base: str) -> str:
    """Link standalone Git hashes while retaining inline-code styling."""
    parts = _HTML_PART_RE.split(html)
    excluded: list[str] = []
    rendered: list[str] = []
    quoted_repo = urllib.parse.quote(repo_slug, safe="")
    for part in parts:
        tag = re.match(r"</?([A-Za-z0-9]+)", part)
        if tag:
            name = tag.group(1).lower()
            if part.startswith("</"):
                if excluded and excluded[-1] == name:
                    excluded.pop()
            elif name in {"a", "pre"} and not part.endswith("/>"):
                excluded.append(name)
            rendered.append(part)
            continue
        if excluded:
            rendered.append(part)
            continue

        def replace(match: re.Match[str]) -> str:
            commit = urllib.parse.quote(match.group("hash"), safe="")
            href = f"{cgit_base}/{quoted_repo}/commit/?id={commit}"
            return f'<a href="{href}">{match.group(0)}</a>'

        rendered.append(_COMMIT_HASH_RE.sub(replace, part))
    return "".join(rendered)


def render_markdown(
    source: str, repo_slug: str | None = None, cgit_base: str | None = None
) -> Markup:
    """Render a plain-text Markdown fragment through a strict allowlist."""
    protected_source, escaped_prefix, escaped_references = _protect_escaped_references(source)
    rendered = _MARKDOWN(protected_source)
    sanitized = _CLEANER.clean(rendered)
    with_rels = _add_external_rel(sanitized)
    normalized = _normalize_headings(with_rels)
    if repo_slug:
        normalized = _linkify_references(normalized, repo_slug)
        if cgit_base:
            normalized = _linkify_commit_hashes(normalized, repo_slug, cgit_base)
    for index, reference in enumerate(escaped_references):
        normalized = normalized.replace(f"{escaped_prefix}{index}END", reference)
    return Markup(normalized)
