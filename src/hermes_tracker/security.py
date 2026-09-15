"""Application-owned response policy, including the outermost error responses."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .domain import ESTABLISHED_LABEL_COLORS, UNKNOWN_LABEL_COLOR, TodoStatus

SWAGGER_JS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
SWAGGER_CSS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"


def _hash_source(source: str) -> str:
    digest = hashlib.sha256(source.encode()).digest()
    return "'sha256-" + base64.b64encode(digest).decode() + "'"


# Only server-owned palette declarations are permitted as inline attributes.
# The bundled SVG also contains a fixed stylesheet and presentation attribute.
_SVG = (Path(__file__).parent / "web" / "static" / "chi-rho.svg").read_text()
_STYLE_ATTRIBUTES = [
    *(f"--status-color: var(--status-{status.value})" for status in TodoStatus),
    *(
        f"--label-color: {color}"
        for color in {*ESTABLISHED_LABEL_COLORS.values(), UNKNOWN_LABEL_COLOR}
    ),
    *re.findall(r'style="([^"]+)"', _SVG),
]
_STYLE_HASHES = " ".join(sorted({_hash_source(style) for style in _STYLE_ATTRIBUTES}))
_SVG_STYLE_HASHES = " ".join(
    # Browsers hash parsed XML text, without CDATA delimiters but with whitespace.
    _hash_source("".join(style.itertext()))
    for style in ElementTree.fromstring(_SVG).iter("{http://www.w3.org/2000/svg}style")
)


def content_security_policy(docs_html: str | None = None) -> str:
    """Keep public pages same-origin; grant Swagger only its own required sources."""
    scripts = "'self'"
    styles = f"'self' {_SVG_STYLE_HASHES}"
    attributes = f"'unsafe-hashes' {_STYLE_HASHES}"
    images = "'self'"
    if docs_html is not None:
        scripts += f" {SWAGGER_JS} " + " ".join(
            _hash_source(script)
            for script in re.findall(r"<script>(.*?)</script>", docs_html, re.S)
        )
        styles += f" {SWAGGER_CSS}"
        # Swagger builds dynamic style attributes. Do not grant this to public pages.
        attributes = "'unsafe-inline'"
        images += " data:"
    return "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            f"script-src {scripts}",
            "script-src-attr 'none'",
            f"style-src {styles}",
            f"style-src-attr {attributes}",
            f"img-src {images}",
            "font-src 'self'",
            "connect-src 'self'",
        )
    )


BASELINE_CSP = content_security_policy()


class SecurityHeadersMiddleware:
    """Wrap ServerErrorMiddleware as well as routing, static and HEAD responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                # Only the authenticated docs route supplies its specialized policy.
                headers.setdefault("Content-Security-Policy", BASELINE_CSP)
            await send(message)

        await self.app(scope, receive, secure)
