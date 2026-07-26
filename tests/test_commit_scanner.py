"""Behavior tests for canonical Git commit directive scanning."""

from __future__ import annotations

import subprocess
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

from hermes_tracker.commit_scanner import (
    Directive,
    GitRepository,
    RepositoryConfig,
    ScanError,
    TrackerClient,
    load_state,
    parse_directives,
    save_state,
    scan_repository,
)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, message: str) -> str:
    tracked = repository / "tracked.txt"
    tracked.write_text(tracked.read_text() + message + "\n" if tracked.exists() else message + "\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "-b", "master")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    _commit(repository, "Initial")
    return repository


@pytest.mark.parametrize(
    "keyword",
    [
        "fix",
        "fixes",
        "fixed",
        "close",
        "closes",
        "closed",
        "resolve",
        "resolves",
        "resolved",
    ],
)
def test_parse_directives_accepts_case_insensitive_supported_keywords(keyword: str) -> None:
    mixed_case = "".join(
        character.upper() if index % 2 else character for index, character in enumerate(keyword)
    )

    assert parse_directives(f"Subject\n\n{mixed_case} #12") == [
        Directive(keyword=keyword, repo_slug=None, number=12, reference="#12")
    ]


def test_parse_directives_supports_cross_repo_and_multiple_references() -> None:
    assert parse_directives("Fixes #2, other.repo#3, and third-repo#4\nCloses #5") == [
        Directive(keyword="fixes", repo_slug=None, number=2, reference="#2"),
        Directive(keyword="fixes", repo_slug="other.repo", number=3, reference="other.repo#3"),
        Directive(keyword="fixes", repo_slug="third-repo", number=4, reference="third-repo#4"),
        Directive(keyword="closes", repo_slug=None, number=5, reference="#5"),
    ]


def test_parse_directives_keeps_vague_words_and_incidental_refs_inert() -> None:
    assert (
        parse_directives(
            "Done #1. Complete #2. Addresses #3. Refs #4. Prefixes #5. "
            "Fixes #6suffix. Resolves repo#7suffix."
        )
        == []
    )


class RecordingClient:
    def __init__(self, fail_number: int | None = None) -> None:
        self.calls: list[tuple[str, int, str, str]] = []
        self.fail_number = fail_number

    def close_from_directive(
        self, repo_slug: str, number: int, commit_sha: str, evidence: str
    ) -> None:
        self.calls.append((repo_slug, number, commit_sha, evidence))
        if number == self.fail_number:
            raise ScanError("closure-failed")


def test_first_sight_baselines_then_fast_forward_scans_new_commits(
    git_repository: Path,
) -> None:
    config = RepositoryConfig("project", git_repository / ".git", "refs/heads/master")
    repository = GitRepository(config.git_path)
    client = RecordingClient()

    baseline = scan_repository(config, None, repository, client)
    first = _commit(git_repository, "Fixes #7")
    second = _commit(git_repository, "Follow-up\n\nResolves other#8 and #9")
    advanced = scan_repository(config, baseline, repository, client)

    assert advanced == second
    assert client.calls == [
        ("project", 7, first, "Canonical commit directive: fixes #7."),
        ("other", 8, second, "Canonical commit directive: resolves other#8."),
        ("project", 9, second, "Canonical commit directive: resolves #9."),
    ]


def test_failed_closure_does_not_advance_cursor_and_retry_replays_range(
    git_repository: Path,
) -> None:
    config = RepositoryConfig("project", git_repository / ".git", "refs/heads/master")
    repository = GitRepository(config.git_path)
    baseline = repository.tip(config.canonical_ref)
    tip = _commit(git_repository, "Fixes #1, #2")
    failing_client = RecordingClient(fail_number=2)

    with pytest.raises(ScanError, match="closure-failed"):
        scan_repository(config, baseline, repository, failing_client)

    retry_client = RecordingClient()
    assert scan_repository(config, baseline, repository, retry_client) == tip
    assert [call[1] for call in failing_client.calls] == [1, 2]
    assert [call[1] for call in retry_client.calls] == [1, 2]


def test_non_fast_forward_fails_closed_until_explicit_rebaseline(git_repository: Path) -> None:
    config = RepositoryConfig("project", git_repository / ".git", "refs/heads/master")
    repository = GitRepository(config.git_path)
    baseline = _commit(git_repository, "Fixes #1")
    _git(git_repository, "reset", "--hard", f"{baseline}^")
    rewritten = _commit(git_repository, "Replacement")

    with pytest.raises(ScanError, match="non-fast-forward"):
        scan_repository(config, baseline, repository, RecordingClient())

    client = RecordingClient()
    assert scan_repository(config, baseline, repository, client, rebaseline=True) == rewritten
    assert client.calls == []


def test_git_process_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(*args, **kwargs):
        raise OSError("private path")

    monkeypatch.setattr("subprocess.run", fail)

    with pytest.raises(ScanError, match="git-command-failed"):
        GitRepository(tmp_path / "private.git").tip("refs/heads/master")


class StubResponse(BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> StubResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class StubOpener:
    def __init__(self, callback) -> None:
        self.callback = callback

    def open(self, request, timeout):
        return self.callback(request, timeout)


def _http_error(request, status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(request.full_url, status, "failure", {}, BytesIO(body))


def test_tracker_client_closes_with_stable_idempotency_and_public_safe_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if request.method == "GET":
            return StubResponse(200, b'{"status":"active"}')
        return StubResponse(200, b'{"status":"done"}')

    client = TrackerClient("http://127.0.0.1:8003", "secret", opener=StubOpener(urlopen))

    client.close_from_directive(
        "other-repo",
        4,
        "a" * 40,
        "Canonical commit directive: fixes other-repo#4.",
    )

    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[1].headers["Idempotency-key"]
    assert requests[1].data == (
        b'{"verification_output": "Canonical commit directive: fixes other-repo#4.", '
        b'"closing_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    )


@pytest.mark.parametrize("status", ["missing", "done"])
def test_tracker_client_treats_missing_and_done_todos_as_noops(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if status == "missing":
            raise _http_error(request, 404, b'{"detail":"missing"}')
        return StubResponse(200, b'{"status":"done"}')

    client = TrackerClient("http://localhost:8003", "secret", opener=StubOpener(urlopen))
    client.close_from_directive("repo", 1, "b" * 40, "Canonical commit directive: closes #1.")

    assert len(requests) == 1


def test_tracker_client_keeps_blocked_closure_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        if request.method == "POST":
            raise _http_error(request, 409, b'{"detail":"blocked"}')
        return StubResponse(200, b'{"status":"active"}')

    with pytest.raises(ScanError, match="tracker-close-failed"):
        client = TrackerClient("http://[::1]:8003", "secret", opener=StubOpener(urlopen))
        client.close_from_directive("repo", 1, "c" * 40, "Canonical commit directive: resolves #1.")

    assert [request.method for request in requests] == ["GET", "POST", "GET"]


def test_tracker_client_disables_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = []

    def build_opener(*values):
        handlers.extend(values)
        return StubOpener(lambda request, timeout: StubResponse(404, b"{}"))

    monkeypatch.setattr("urllib.request.build_opener", build_opener)

    TrackerClient("http://127.0.0.1:8003", "secret")

    proxy_handler = next(
        handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)
    )
    assert proxy_handler.proxies == {}


def test_state_round_trips_atomically(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cursor.json"
    state = {"repo": "d" * 40}

    save_state(path, state)

    assert load_state(path) == state
    assert list(path.parent.iterdir()) == [path]
