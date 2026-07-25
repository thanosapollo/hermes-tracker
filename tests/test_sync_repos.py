"""Tests for deterministic public-cgit repository synchronization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermes_tracker.sync_repos import (
    PublicRepo,
    SyncEvent,
    main,
    parse_manifest,
    sync_repositories,
)


@dataclass
class FakeClient:
    repos: dict[str, dict[str, object]] = field(default_factory=dict)
    failures: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def list_repos(self) -> list[dict[str, object]]:
        return list(self.repos.values())

    def create_repo(self, repo: PublicRepo) -> None:
        self.calls.append(("create", repo.slug))
        if repo.slug in self.failures:
            raise OSError("unavailable")
        self.repos[repo.slug] = {
            "id": len(self.repos) + 1,
            "slug": repo.slug,
            "name": repo.name,
            "description": repo.description,
            "navigation": [entry.as_dict() for entry in repo.navigation],
            "published": True,
            "created_at": "2026-01-01T00:00:00Z",
        }

    def update_repo(self, repo: PublicRepo) -> None:
        self.calls.append(("update", repo.slug))
        if repo.slug in self.failures:
            raise OSError("unavailable")
        self.repos[repo.slug].update(
            name=repo.name,
            description=repo.description,
            navigation=[entry.as_dict() for entry in repo.navigation],
            published=True,
        )

    def unpublish_repo(self, slug: str) -> None:
        self.calls.append(("unpublish", slug))
        if slug in self.failures:
            raise OSError("unavailable")
        self.repos[slug]["published"] = False


def test_parse_manifest_preserves_exact_repository_navigation_order() -> None:
    labels = ["about", "summary", "refs", "log", "tree", "commit", "diff", "homepage", "todos"]
    navigation = [
        {"label": label, "url": f"https://git.thanosapollo.org/hermes-tracker/{label}"}
        for label in labels[:-1]
    ] + [{"label": "todos", "url": "/r/hermes-tracker"}]
    records, events = parse_manifest(
        [{"slug": "hermes-tracker", "name": "Tracker", "navigation": navigation}]
    )
    assert events == []
    assert [entry.label for entry in records[0].navigation] == labels


@pytest.mark.parametrize(
    "navigation",
    [
        [{"label": "evil<script>", "url": "https://git.thanosapollo.org/x"}],
        [{"label": "summary", "url": "javascript:alert(1)"}],
        [{"label": "summary", "url": "//evil.example/x"}],
        [{"label": "summary", "url": "https://user:pass@example.com/x"}],
        [{"label": "summary", "url": "https://example.com/x#fragment"}],
        [
            {"label": "summary", "url": "https://example.com/x"},
            {"label": "summary", "url": "https://example.com/y"},
        ],
    ],
)
def test_parse_manifest_rejects_unsafe_navigation(navigation: object) -> None:
    records, events = parse_manifest(
        [{"slug": "unsafe", "name": "Unsafe", "navigation": navigation}]
    )
    assert records == []
    assert events == [SyncEvent("unsafe", "invalid", "invalid-navigation")]


def test_sync_unpublishes_repositories_absent_from_public_manifest() -> None:
    client = FakeClient(
        repos={
            "private-ops": {
                "id": 1,
                "slug": "private-ops",
                "name": "Private",
                "description": None,
                "published": True,
                "created_at": "2026-01-01T00:00:00Z",
            }
        }
    )

    events = sync_repositories([PublicRepo("gnosis", "Gnosis", "Knowledge system")], client)

    assert client.calls == [("create", "gnosis"), ("unpublish", "private-ops")]
    assert client.repos["private-ops"]["name"] == "Private"
    assert client.repos["private-ops"]["published"] is False
    assert events == [
        SyncEvent("gnosis", "created"),
        SyncEvent("private-ops", "unpublished", "absent-from-public-manifest"),
    ]


@pytest.mark.parametrize(
    "manifest_value",
    [
        {},
        [{"slug": "valid", "name": "Valid"}, {"slug": "broken"}],
    ],
)
def test_main_invalid_manifest_never_authorizes_absence_unpublication(
    manifest_value: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "repos.json"
    manifest.write_text(json.dumps(manifest_value))
    client = FakeClient(
        repos={
            "kept": {
                "slug": "kept",
                "name": "Kept",
                "description": None,
                "navigation": [],
                "published": True,
            }
        }
    )
    monkeypatch.setenv("HERMES_TRACKER_AUTH_TOKEN", "secret")
    monkeypatch.setattr("hermes_tracker.sync_repos.TrackerClient", lambda *_args: client)

    assert main(["--manifest", str(manifest)]) == 1

    assert ("unpublish", "kept") not in client.calls
    assert client.repos["kept"]["published"] is True


def test_sync_is_idempotent() -> None:
    client = FakeClient()
    manifest = [PublicRepo("gnosis", "Gnosis", "Knowledge system")]

    first = sync_repositories(manifest, client)
    client.calls.clear()
    second = sync_repositories(manifest, client)

    assert first == [SyncEvent("gnosis", "created")]
    assert second == [SyncEvent("gnosis", "unchanged")]
    assert client.calls == []


def test_sync_updates_safe_metadata_in_place() -> None:
    client = FakeClient(
        repos={
            "gnosis": {
                "id": 17,
                "slug": "gnosis",
                "name": "Old",
                "description": "Old description",
                "created_at": "2026-01-01T00:00:00Z",
            }
        }
    )

    events = sync_repositories([PublicRepo("gnosis", "Gnosis", "Knowledge system")], client)

    assert events == [SyncEvent("gnosis", "updated")]
    assert client.repos["gnosis"]["id"] == 17
    assert client.repos["gnosis"]["name"] == "Gnosis"


def test_parse_manifest_reports_malformed_and_duplicate_records() -> None:
    records, events = parse_manifest(
        [
            {"slug": "gnosis", "name": "Gnosis", "description": "Knowledge"},
            {"slug": "bad slug", "name": "Bad"},
            {"slug": "gnosis", "name": "Duplicate"},
            {"slug": "missing-name"},
        ]
    )

    assert records == [PublicRepo("gnosis", "Gnosis", "Knowledge")]
    assert events == [
        SyncEvent("bad slug", "invalid", "invalid-slug"),
        SyncEvent("gnosis", "invalid", "duplicate-slug"),
        SyncEvent("missing-name", "invalid", "invalid-name"),
    ]


def test_parse_manifest_does_not_let_malformed_record_hide_valid_slug() -> None:
    records, events = parse_manifest(
        [
            {"slug": "gnosis"},
            {"slug": "gnosis", "name": "Gnosis", "description": "Knowledge"},
        ]
    )

    assert records == [PublicRepo("gnosis", "Gnosis", "Knowledge")]
    assert events == [SyncEvent("gnosis", "invalid", "invalid-name")]


@pytest.mark.parametrize("value", [None, {}, "not-a-list"])
def test_parse_manifest_rejects_non_list_document(value: object) -> None:
    records, events = parse_manifest(value)

    assert records == []
    assert events == [SyncEvent(None, "invalid", "manifest-must-be-array")]


def test_sync_continues_after_individual_failure() -> None:
    client = FakeClient(failures={"broken"})

    events = sync_repositories(
        [PublicRepo("broken", "Broken", None), PublicRepo("good", "Good", None)],
        client,
    )

    assert client.calls == [("create", "broken"), ("create", "good")]
    assert events == [
        SyncEvent("broken", "error", "OSError"),
        SyncEvent("good", "created"),
    ]


def test_main_returns_nonzero_for_unavailable_tracker_without_leaking_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "repos.json"
    manifest.write_text(json.dumps([{"slug": "gnosis", "name": "Gnosis"}]))
    token = "do-not-print-this-secret"
    monkeypatch.setenv("HERMES_TRACKER_AUTH_TOKEN", token)

    exit_code = main(["--manifest", str(manifest), "--base-url", "http://127.0.0.1:1"])

    output = capsys.readouterr()
    assert exit_code == 1
    assert token not in output.out
    assert token not in output.err
    event = json.loads(output.out)
    assert event["action"] == "error"
    assert event["reason"] in {"URLError", "ConnectionRefusedError"}
