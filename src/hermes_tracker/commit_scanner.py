"""Poll configured canonical Git refs and close TODOs named by commit directives."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF_PATTERN = re.compile(r"^refs/[A-Za-z0-9][A-Za-z0-9_./-]{0,499}$")
_REFERENCE = r"(?:[A-Za-z0-9_.-]+)?#[1-9][0-9]*(?![A-Za-z0-9_.-])"
_DIRECTIVE_PATTERN = re.compile(
    r"\b(?P<keyword>fix(?:es|ed)?|close(?:s|d)?|resolve(?:s|d)?)\s+"
    rf"(?P<references>{_REFERENCE}"
    rf"(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+){_REFERENCE})*)",
    re.IGNORECASE,
)
_REFERENCE_PATTERN = re.compile(
    r"(?:(?P<repo>[A-Za-z0-9_.-]+))?#(?P<number>[1-9][0-9]*)(?![A-Za-z0-9_.-])"
)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ScanError(RuntimeError):
    """Fail-closed scanner error with a public-safe category message."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Directive:
    keyword: str
    repo_slug: str | None
    number: int
    reference: str


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str


@dataclass(frozen=True)
class PlannedClosure:
    repo_slug: str
    number: int
    commit_sha: str
    evidence: str


@dataclass(frozen=True)
class ScanPlan:
    next_sha: str
    closures: tuple[PlannedClosure, ...]


@dataclass(frozen=True)
class RepositoryConfig:
    tracker_slug: str
    git_path: Path
    canonical_ref: str

    def __post_init__(self) -> None:
        if not _SLUG_PATTERN.fullmatch(self.tracker_slug):
            raise ValueError("invalid tracker slug")
        if not _REF_PATTERN.fullmatch(self.canonical_ref) or ".." in self.canonical_ref:
            raise ValueError("invalid canonical ref")

    @property
    def state_key(self) -> str:
        return self._state_key_for(self.git_path.resolve())

    def _state_key_for(self, git_path: Path) -> str:
        return "\0".join((self.tracker_slug, str(git_path), self.canonical_ref))


class ClosureClient(Protocol):
    def close_from_directive(
        self, repo_slug: str, number: int, commit_sha: str, evidence: str
    ) -> None: ...


def parse_directives(message: str) -> list[Directive]:
    """Return supported close directives from a commit subject and body."""
    directives: list[Directive] = []
    for match in _DIRECTIVE_PATTERN.finditer(message):
        keyword = match.group("keyword").lower()
        for reference_match in _REFERENCE_PATTERN.finditer(match.group("references")):
            reference = reference_match.group(0)
            directives.append(
                Directive(
                    keyword=keyword,
                    repo_slug=reference_match.group("repo"),
                    number=int(reference_match.group("number")),
                    reference=reference,
                )
            )
    return directives


class GitRepository:
    """Read-only Git adapter using argv-only subprocess calls."""

    def __init__(self, git_path: Path) -> None:
        self._git_path = git_path

    def _run(
        self, *args: str, allowed_codes: set[int] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "--git-dir", str(self._git_path), *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, UnicodeError) as exc:
            raise ScanError("git-command-failed") from exc
        allowed = allowed_codes or {0}
        if result.returncode not in allowed:
            raise ScanError("git-command-failed")
        return result

    def tip(self, canonical_ref: str) -> str:
        return self._run("rev-parse", "--verify", f"{canonical_ref}^{{commit}}").stdout.strip()

    def is_ancestor(self, old_sha: str, new_sha: str) -> bool:
        result = self._run("merge-base", "--is-ancestor", old_sha, new_sha, allowed_codes={0, 1})
        return result.returncode == 0

    def commits(self, old_sha: str, new_sha: str) -> list[Commit]:
        shas = self._run("rev-list", "--reverse", f"{old_sha}..{new_sha}").stdout.splitlines()
        return [Commit(sha, self._run("show", "-s", "--format=%B", sha).stdout) for sha in shas]


def preflight_repository(
    config: RepositoryConfig,
    previous_sha: str | None,
    repository: GitRepository,
    *,
    rebaseline: bool = False,
) -> ScanPlan:
    """Validate one repository range and return its mutation-free scan plan."""
    tip = repository.tip(config.canonical_ref)
    if previous_sha is None:
        return ScanPlan(tip, ())
    if previous_sha == tip:
        return ScanPlan(tip, ())
    if not repository.is_ancestor(previous_sha, tip):
        if rebaseline:
            return ScanPlan(tip, ())
        raise ScanError("non-fast-forward-requires-rebaseline")

    closures: list[PlannedClosure] = []
    for commit in repository.commits(previous_sha, tip):
        for directive in parse_directives(commit.message):
            repo_slug = directive.repo_slug or config.tracker_slug
            evidence = f"Canonical commit directive: {directive.keyword} {directive.reference}."
            closures.append(PlannedClosure(repo_slug, directive.number, commit.sha, evidence))
    return ScanPlan(tip, tuple(closures))


def apply_scan_plan(plan: ScanPlan, client: ClosureClient) -> str:
    """Apply a validated plan; return its cursor only after every closure succeeds."""
    for closure in plan.closures:
        client.close_from_directive(
            closure.repo_slug,
            closure.number,
            closure.commit_sha,
            closure.evidence,
        )
    return plan.next_sha


def scan_repository(
    config: RepositoryConfig,
    previous_sha: str | None,
    repository: GitRepository,
    client: ClosureClient,
    *,
    rebaseline: bool = False,
) -> str:
    """Scan one fast-forward range; return new cursor only after every closure succeeds."""
    plan = preflight_repository(config, previous_sha, repository, rebaseline=rebaseline)
    return apply_scan_plan(plan, client)


class TrackerClient:
    """Small authenticated client restricted to a loopback Tracker API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        try:
            parsed = urllib.parse.urlsplit(base_url)
            _ = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid base URL") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base URL must be an HTTP loopback address")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, object | None]:
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except (json.JSONDecodeError, UnicodeError):
                payload = None
            return exc.code, payload
        except (OSError, ValueError) as exc:
            raise ScanError("tracker-request-failed") from exc

    def close_from_directive(
        self, repo_slug: str, number: int, commit_sha: str, evidence: str
    ) -> None:
        quoted_slug = urllib.parse.quote(repo_slug, safe="")
        path = f"/api/v1/repos/{quoted_slug}/todos/{number}"
        status_code, todo = self._request("GET", path)
        if status_code == 404:
            return
        if status_code != 200 or not isinstance(todo, dict):
            raise ScanError("tracker-read-failed")
        if todo.get("status") == "done":
            return

        idempotency_key = hashlib.sha256(
            f"commit-directive\0{commit_sha}\0{repo_slug}\0{number}".encode()
        ).hexdigest()
        status_code, _ = self._request(
            "POST",
            f"{path}/close",
            {"verification_output": evidence, "closing_commit": commit_sha},
            idempotency_key,
        )
        if status_code == 200:
            return
        if status_code == 404:
            return
        if status_code == 409:
            retry_code, retry_todo = self._request("GET", path)
            if retry_code == 404 or (
                retry_code == 200
                and isinstance(retry_todo, dict)
                and retry_todo.get("status") == "done"
            ):
                return
        raise ScanError("tracker-close-failed")


def load_config(path: Path) -> list[RepositoryConfig]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScanError("invalid-config") from exc
    if not isinstance(value, list) or not value:
        raise ScanError("invalid-config")
    configs: list[RepositoryConfig] = []
    try:
        for item in value:
            if not isinstance(item, dict) or set(item) != {
                "tracker_slug",
                "git_path",
                "canonical_ref",
            }:
                raise ValueError
            configs.append(
                RepositoryConfig(
                    tracker_slug=item["tracker_slug"],
                    git_path=Path(item["git_path"]),
                    canonical_ref=item["canonical_ref"],
                )
            )
    except (TypeError, ValueError) as exc:
        raise ScanError("invalid-config") from exc
    if len({config.state_key for config in configs}) != len(configs):
        raise ScanError("duplicate-config")
    return configs


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScanError("invalid-state") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha)
        for key, sha in value.items()
    ):
        raise ScanError("invalid-state")
    return value


def save_state(path: Path, state: dict[str, str]) -> None:
    """Atomically replace cursor state and fsync file plus containing directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temporary:
            temporary_name = temporary.name
            json.dump(state, temporary, sort_keys=True, separators=(",", ":"))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if temporary_name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name)
        raise ScanError("state-write-failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--rebaseline", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get("HERMES_TRACKER_AUTH_TOKEN")
    if not token:
        print(json.dumps({"action": "error", "reason": "missing-auth-token"}))
        return 1
    try:
        configs = load_config(args.config)
        state = load_state(args.state)
        client = TrackerClient(args.base_url, token)
        plans: list[tuple[str, ScanPlan]] = []
        for config in configs:
            git_path = config.git_path.resolve()
            state_key = config._state_key_for(git_path)
            plan = preflight_repository(
                config,
                state.get(state_key),
                GitRepository(git_path),
                rebaseline=args.rebaseline,
            )
            plans.append((state_key, plan))
        next_state = state.copy()
        for state_key, plan in plans:
            next_state[state_key] = apply_scan_plan(plan, client)
        save_state(args.state, next_state)
    except (ScanError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ScanError) else "invalid-settings"
        print(json.dumps({"action": "error", "reason": reason}, sort_keys=True))
        return 1
    print(json.dumps({"action": "ok", "repositories": len(configs)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
