"""Pure parsing of explicit commit-closing directives."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIRECTIVE = re.compile(r"(?im)^\s*(?:fix(?:e[sd])?|clos(?:e[sd])?|resolv(?:e[sd])?)\s+([^\n]+)$")
_REFERENCE = re.compile(r"(?:(?P<repo>[A-Za-z0-9_.-]+))?#(?P<number>[1-9][0-9]*)")


@dataclass(frozen=True, slots=True)
class Directive:
    repo_slug: str | None
    number: int


def parse_directives(message: str) -> list[Directive]:
    result: list[Directive] = []
    for match in _DIRECTIVE.finditer(message):
        text = match.group(1)
        references = list(_REFERENCE.finditer(text))
        if not references:
            continue
        remainder = _REFERENCE.sub("", text)
        if re.sub(r"[\s,.;:()]+|\band\b", "", remainder, flags=re.IGNORECASE):
            continue
        result.extend(
            Directive(item.group("repo"), int(item.group("number"))) for item in references
        )
    return result
