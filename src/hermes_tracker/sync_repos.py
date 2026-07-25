"""Synchronize an explicit public-cgit manifest into Hermes Tracker repositories."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

_SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")
_NAVIGATION_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class NavigationEntry:
    label: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "url": self.url}


@dataclass(frozen=True)
class PublicRepo:
    slug: str
    name: str
    description: str | None
    navigation: tuple[NavigationEntry, ...] = ()


@dataclass(frozen=True)
class SyncEvent:
    slug: str | None
    action: str
    reason: str | None = None


class RepoClient(Protocol):
    def list_repos(self) -> list[dict[str, object]]: ...

    def create_repo(self, repo: PublicRepo) -> None: ...

    def update_repo(self, repo: PublicRepo) -> None: ...

    def unpublish_repo(self, slug: str) -> None: ...


class TrackerClient:
    """Small authenticated JSON client restricted to a loopback tracker API."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError("base URL must be an HTTP loopback address")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, object] | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.load(response)

    def list_repos(self) -> list[dict[str, object]]:
        value = self._request("GET", "/api/v1/repos")
        if not isinstance(value, list):
            raise ValueError("Hermes Tracker repository response must be an array")
        return [item for item in value if isinstance(item, dict)]

    def create_repo(self, repo: PublicRepo) -> None:
        self._request(
            "POST",
            "/api/v1/repos",
            {
                "slug": repo.slug,
                "name": repo.name,
                "description": repo.description,
                "navigation": [entry.as_dict() for entry in repo.navigation],
            },
        )

    def update_repo(self, repo: PublicRepo) -> None:
        slug = urllib.parse.quote(repo.slug, safe="")
        self._request(
            "PATCH",
            f"/api/v1/repos/{slug}",
            {
                "name": repo.name,
                "description": repo.description,
                "navigation": [entry.as_dict() for entry in repo.navigation],
                "published": True,
            },
        )

    def unpublish_repo(self, slug: str) -> None:
        quoted_slug = urllib.parse.quote(slug, safe="")
        self._request("PATCH", f"/api/v1/repos/{quoted_slug}", {"published": False})


def _safe_navigation_url(value: str) -> bool:
    if not value or len(value) > 2000 or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except (UnicodeError, ValueError):
        return False
    if value.startswith("/") and not value.startswith("//"):
        return not parsed.query and not parsed.fragment
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _parse_navigation(value: object) -> tuple[NavigationEntry, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 50:
        return None
    entries: list[NavigationEntry] = []
    labels: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "url"}:
            return None
        label = item["label"]
        url = item["url"]
        if not isinstance(label, str) or not _NAVIGATION_LABEL.fullmatch(label):
            return None
        if label in labels or not isinstance(url, str) or not _safe_navigation_url(url):
            return None
        labels.add(label)
        entries.append(NavigationEntry(label, url))
    return tuple(entries)


def parse_manifest(value: object) -> tuple[list[PublicRepo], list[SyncEvent]]:
    """Validate records independently so one malformed entry cannot hide valid ones."""
    if not isinstance(value, list):
        return [], [SyncEvent(None, "invalid", "manifest-must-be-array")]

    repos: list[PublicRepo] = []
    events: list[SyncEvent] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            events.append(SyncEvent(None, "invalid", "record-must-be-object"))
            continue
        slug = item.get("slug")
        event_slug = slug if isinstance(slug, str) else None
        if not isinstance(slug, str) or not _SLUG_PATTERN.fullmatch(slug):
            events.append(SyncEvent(event_slug, "invalid", "invalid-slug"))
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 200:
            events.append(SyncEvent(slug, "invalid", "invalid-name"))
            continue
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            events.append(SyncEvent(slug, "invalid", "invalid-description"))
            continue
        navigation = _parse_navigation(item.get("navigation"))
        if navigation is None:
            events.append(SyncEvent(slug, "invalid", "invalid-navigation"))
            continue
        if slug in seen:
            events.append(SyncEvent(slug, "invalid", "duplicate-slug"))
            continue
        seen.add(slug)
        repos.append(PublicRepo(slug, name, description, navigation))
    return repos, events


def sync_repositories(
    repos: list[PublicRepo], client: RepoClient, *, authoritative: bool = True
) -> list[SyncEvent]:
    """Synchronize validated records, withdrawing absences only from an authoritative manifest."""
    existing = {str(repo["slug"]): repo for repo in client.list_repos()}
    events: list[SyncEvent] = []
    public_slugs = {repo.slug for repo in repos}
    for repo in repos:
        current = existing.get(repo.slug)
        try:
            if current is None:
                client.create_repo(repo)
                events.append(SyncEvent(repo.slug, "created"))
            elif (
                current.get("name") != repo.name
                or current.get("description") != repo.description
                or current.get("navigation") != [entry.as_dict() for entry in repo.navigation]
                or current.get("published") is not True
            ):
                client.update_repo(repo)
                events.append(SyncEvent(repo.slug, "updated"))
            else:
                events.append(SyncEvent(repo.slug, "unchanged"))
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            events.append(SyncEvent(repo.slug, "error", type(exc).__name__))

    if not authoritative:
        return events

    for slug in sorted(existing.keys() - public_slugs):
        if existing[slug].get("published") is False:
            events.append(SyncEvent(slug, "unchanged", "absent-from-public-manifest"))
            continue
        try:
            client.unpublish_repo(slug)
            events.append(SyncEvent(slug, "unpublished", "absent-from-public-manifest"))
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            events.append(SyncEvent(slug, "error", type(exc).__name__))
    return events


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    return parser


def _emit(event: SyncEvent) -> None:
    print(json.dumps(asdict(event), sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get("HERMES_TRACKER_AUTH_TOKEN")
    if not token:
        _emit(SyncEvent(None, "error", "missing-auth-token"))
        return 1

    try:
        value: Any = json.loads(args.manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _emit(SyncEvent(None, "error", type(exc).__name__))
        return 1

    repos, events = parse_manifest(value)
    try:
        client = TrackerClient(args.base_url, token)
        events.extend(
            sync_repositories(
                repos,
                client,
                authoritative=not any(event.action == "invalid" for event in events),
            )
        )
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        events.append(SyncEvent(None, "error", type(exc).__name__))

    for event in events:
        _emit(event)
    return int(any(event.action in {"invalid", "error"} for event in events))


if __name__ == "__main__":
    sys.exit(main())
