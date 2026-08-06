"""Independent read-only verifier for v8-to-v9 conversion artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .db import validate_schema
from .domain import normalize_labels
from .migration_io import (
    BOUND_EVIDENCE_FILES,
    EVIDENCE_FILES,
    UnsafeMigrationPath,
    directory_chain_matches,
    evidence_binding,
    hash_fd,
    open_directory_chain,
    open_private_directory_at,
    open_regular_at,
    read_file_at,
    reject_sidecars,
    require_private_directory,
    sibling_names,
    write_file_at,
)
from .models import NavigationItem
from .v8_schema import EXPECTED_V8_FINGERPRINT, structural_fingerprint

RETAINED_SCOPES = frozenset({"read", "repo:manage", "todo:create", "todo:update", "comment:create"})
FULL_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
RETAINED_SEQUENCES = ("repositories", "todos", "comments", "agent_tokens")
DROPPED_TABLES = (
    "external_references",
    "mutation_receipts",
    "superseded_kanban_executions",
    "todo_links",
    "tracker_event_deliveries",
    "tracker_events",
)


class VerificationError(RuntimeError):
    pass


def _valid_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _array(raw: str, name: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"malformed {name}") from exc
    if not isinstance(value, list):
        raise VerificationError(f"malformed {name}")
    return value


def _labels(raw: str) -> list[str]:
    value = _array(raw, "labels")
    if any(not isinstance(item, str) for item in value):
        raise VerificationError("malformed labels")
    normalized = normalize_labels(value)
    if normalized != value:
        raise VerificationError("noncanonical labels")
    return normalized


def _navigation(raw: str) -> list[dict[str, str]]:
    value = _array(raw, "navigation")
    if any(
        not isinstance(item, dict)
        or set(item) != {"label", "url"}
        or not isinstance(item["label"], str)
        or not isinstance(item["url"], str)
        for item in value
    ):
        raise VerificationError("malformed navigation")
    try:
        parsed = [NavigationItem.model_validate(item) for item in value]
    except ValueError as exc:
        raise VerificationError("malformed navigation") from exc
    labels = [item.label for item in parsed]
    if len(parsed) > 50 or len(labels) != len(set(labels)):
        raise VerificationError("malformed navigation")
    return cast(list[dict[str, str]], value)


def _scopes(raw: str) -> list[str]:
    value = _array(raw, "token scopes")
    if any(not isinstance(item, str) for item in value):
        raise VerificationError("malformed token scopes")
    return sorted(set(value) & RETAINED_SCOPES)


def _destination_scopes(raw: str) -> list[str]:
    value = _array(raw, "token scopes")
    if any(not isinstance(item, str) for item in value):
        raise VerificationError("malformed token scopes")
    if value != sorted(set(value)) or any(item not in RETAINED_SCOPES for item in value):
        raise VerificationError("noncanonical destination token scopes")
    return cast(list[str], value)


def _source_rows(conn: sqlite3.Connection, disabled_at: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    repo_slugs: dict[int, str] = {}
    for row in conn.execute("SELECT * FROM repositories ORDER BY id"):
        repo_slugs[row["id"]] = row["slug"]
        result.append(
            {
                "kind": "repository",
                "id": row["id"],
                "slug": row["slug"],
                "name": row["name"],
                "description": row["description"],
                "navigation": _navigation(row["navigation"]),
                "published": row["published"],
                "created_at": row["created_at"],
            }
        )
    result.extend(
        {"kind": "alias", **dict(row)}
        for row in conn.execute("SELECT * FROM repository_aliases ORDER BY alias_slug")
    )
    for row in conn.execute("SELECT * FROM todos ORDER BY repo_id,number"):
        if row["status"] not in {"open", "active", "blocked", "done"}:
            raise VerificationError("malformed todo status")
        status = "closed" if row["status"] == "done" else "open"
        result.append(
            {
                "kind": "todo",
                "id": row["id"],
                "repo_id": row["repo_id"],
                "number": row["number"],
                "title": row["title"],
                "description": row["description"],
                "status": status,
                "priority": row["priority"],
                "assignee": row["assignee"],
                "author": row["author"],
                "labels": _labels(row["labels"]),
                "verification_output": row["verification_output"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        commit_sha = row["closing_commit"]
        closed_at = _valid_timestamp(row["closed_at"])
        if status == "closed" and FULL_OBJECT_ID.fullmatch(commit_sha or "") and closed_at:
            result.append(
                {
                    "kind": "closure",
                    "todo_id": row["id"],
                    "commit_sha": commit_sha,
                    "source_repo": repo_slugs[row["repo_id"]],
                    "source_ref": "refs/heads/legacy-migration",
                    "closed_by": row["closed_by"] or "migration",
                    "closed_at": closed_at,
                }
            )
    result.extend(
        {"kind": "comment", **dict(row)}
        for row in conn.execute("SELECT * FROM comments ORDER BY id")
    )
    for row in conn.execute("SELECT * FROM agent_tokens ORDER BY id"):
        scopes = _scopes(row["scopes"])
        result.append(
            {
                "kind": "token",
                "id": row["id"],
                "token_prefix": row["token_prefix"],
                "actor": row["actor"],
                "token_hash": row["token_hash"],
                "scopes": scopes,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "disabled_at": row["disabled_at"]
                or (disabled_at if not scopes or row["actor"] == "commit-scanner" else None),
                "last_used_at": row["last_used_at"],
            }
        )
    result.extend(_sequence_rows(conn))
    return result


def _destination_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    result.extend(
        {"kind": "repository", **dict(row), "navigation": _navigation(row["navigation"])}
        for row in conn.execute("SELECT * FROM repositories ORDER BY id")
    )
    result.extend(
        {"kind": "alias", **dict(row)}
        for row in conn.execute("SELECT * FROM repository_aliases ORDER BY alias_slug")
    )
    result.extend(
        {"kind": "todo", **dict(row), "labels": _labels(row["labels"])}
        for row in conn.execute("SELECT * FROM todos ORDER BY repo_id,number")
    )
    result.extend(
        {"kind": "closure", **dict(row)}
        for row in conn.execute("SELECT * FROM todo_closures ORDER BY todo_id")
    )
    result.extend(
        {"kind": "comment", **dict(row)}
        for row in conn.execute("SELECT * FROM comments ORDER BY id")
    )
    result.extend(
        {"kind": "token", **dict(row), "scopes": _destination_scopes(row["scopes"])}
        for row in conn.execute("SELECT * FROM agent_tokens ORDER BY id")
    )
    result.extend(_sequence_rows(conn))
    return result


def _sequence_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for table in RETAINED_SEQUENCES:
        maximum = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        if row is None:
            if maximum is not None:
                raise VerificationError(f"missing sqlite_sequence for {table}")
            continue
        sequence = row[0]
        if (
            not isinstance(sequence, int)
            or sequence < 0
            or (maximum is not None and sequence < maximum)
        ):
            raise VerificationError(f"invalid sqlite_sequence for {table}")
        result.append({"kind": "sequence", "table": table, "value": sequence})
    return result


def _content(rows: list[dict[str, Any]]) -> tuple[str, str]:
    lines = []
    for row in rows:
        row_text = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(row_text.encode()).hexdigest()
        lines.append(
            json.dumps({**row, "row_sha256": digest}, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
    text = "".join(lines)
    return text, hashlib.sha256(text.encode()).hexdigest()


def _validate_source_schema(conn: sqlite3.Connection) -> None:
    versions = [
        row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    if (
        versions != list(range(1, 9))
        or structural_fingerprint(conn) != EXPECTED_V8_FINGERPRINT
        or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        or conn.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise VerificationError("source is not exact v8 schema")


def verify_open_fds(
    source_fd: int,
    destination_fd: int,
    evidence_fd: int,
    uid: int,
    gid: int,
    *,
    publish_evidence: bool,
) -> dict[str, Any]:
    """Verify already anchored files; optionally publish evidence in an unpublished directory."""
    source_conn = sqlite3.connect(f"file:/proc/self/fd/{source_fd}?mode=ro&immutable=1", uri=True)
    destination_conn = sqlite3.connect(
        f"file:/proc/self/fd/{destination_fd}?mode=ro&immutable=1", uri=True
    )
    source_conn.row_factory = destination_conn.row_factory = sqlite3.Row
    source_file_hash = hash_fd(source_fd)
    destination_file_hash = hash_fd(destination_fd)
    try:
        _validate_source_schema(source_conn)
        validate_schema(destination_conn)
        disabled_at = destination_conn.execute(
            "SELECT applied_at FROM schema_migrations WHERE version=9"
        ).fetchone()[0]
        source_rows = _source_rows(source_conn, disabled_at)
        destination_rows = _destination_rows(destination_conn)
        source_text, source_hash = _content(source_rows)
        destination_text, destination_hash = _content(destination_rows)
        dropped_rows = {
            table: source_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in DROPPED_TABLES
        }
    finally:
        source_conn.close()
        destination_conn.close()

    try:
        report = json.loads(read_file_at(evidence_fd, "converter-report.json", uid, gid))
        converter_source = read_file_at(evidence_fd, "converter-source.jsonl", uid, gid)
        converter_destination = read_file_at(evidence_fd, "converter-destination.jsonl", uid, gid)
    except (OSError, json.JSONDecodeError, UnsafeMigrationPath) as exc:
        raise VerificationError("converter manifests unavailable") from exc
    if not (
        source_hash
        == destination_hash
        == report.get("source_manifest_sha256")
        == report.get("destination_manifest_sha256")
        and source_text == converter_source
        and destination_text == converter_destination
        and report.get("retained_rows") == len(source_rows)
        and report.get("retained_counts")
        == dict(sorted(Counter(row["kind"] for row in source_rows).items()))
        and report.get("dropped_rows") == dropped_rows
        and report.get("source_file_sha256") == source_file_hash
        and report.get("destination_file_sha256") == destination_file_hash
        and report.get("migration_applied_at") == disabled_at
    ):
        raise VerificationError("manifest mismatch")

    result: dict[str, Any] = {
        "verified": True,
        "manifest_sha256": source_hash,
        "source_file_sha256": source_file_hash,
        "destination_file_sha256": destination_file_hash,
        "retained_rows": len(source_rows),
    }
    verifier_manifests = {
        "verifier-source.jsonl": source_text,
        "verifier-destination.jsonl": destination_text,
    }
    if publish_evidence:
        for name, content in verifier_manifests.items():
            write_file_at(evidence_fd, name, content, uid, gid)
        os.fsync(evidence_fd)
        result.update(
            evidence_binding(
                evidence_fd,
                destination_fd,
                uid,
                gid,
                exact_names=BOUND_EVIDENCE_FILES,
            )
        )
        write_file_at(
            evidence_fd,
            "verifier-report.json",
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
            uid,
            gid,
        )
        os.fsync(evidence_fd)
    else:
        for name, content in verifier_manifests.items():
            if read_file_at(evidence_fd, name, uid, gid) != content:
                raise VerificationError("verifier evidence mismatch")
        result.update(
            evidence_binding(
                evidence_fd,
                destination_fd,
                uid,
                gid,
                exact_names=EVIDENCE_FILES,
            )
        )
        try:
            verifier_report = json.loads(
                read_file_at(evidence_fd, "verifier-report.json", uid, gid)
            )
        except json.JSONDecodeError as exc:
            raise VerificationError("verifier evidence mismatch") from exc
        if verifier_report != result:
            raise VerificationError("verifier evidence mismatch")
    return result


def _final_snapshot(
    parent: Path,
    parent_fd: int,
    source_name: str,
    destination_name: str,
    evidence_name: str,
    source_fd: int,
    destination_fd: int,
    evidence_fd: int,
    uid: int,
    gid: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return only a final path/content snapshot bound to verified descriptors."""
    opened: list[int] = []
    try:
        if not directory_chain_matches(parent, parent_fd):
            raise VerificationError("migration parent changed during verification")
        final_source_fd, _ = open_regular_at(parent_fd, source_name, uid, gid)
        opened.append(final_source_fd)
        final_destination_fd, _ = open_regular_at(parent_fd, destination_name, uid, gid)
        opened.append(final_destination_fd)
        final_evidence_fd = open_private_directory_at(parent_fd, evidence_name, uid, gid)
        opened.append(final_evidence_fd)
        final_database_fd, _ = open_regular_at(final_evidence_fd, "database.db", uid, gid)
        opened.append(final_database_fd)
        if any(
            (os.fstat(final).st_dev, os.fstat(final).st_ino)
            != (os.fstat(anchored).st_dev, os.fstat(anchored).st_ino)
            for final, anchored in (
                (final_source_fd, source_fd),
                (final_destination_fd, destination_fd),
                (final_evidence_fd, evidence_fd),
                (final_database_fd, destination_fd),
            )
        ):
            raise VerificationError("migration path changed during verification")
        reject_sidecars(parent_fd, source_name)
        reject_sidecars(parent_fd, destination_name)
        binding = evidence_binding(
            final_evidence_fd,
            final_database_fd,
            uid,
            gid,
            exact_names=EVIDENCE_FILES,
        )
        if any(result.get(name) != value for name, value in binding.items()):
            raise VerificationError("verifier evidence mismatch")
        if (
            hash_fd(final_source_fd) != result["source_file_sha256"]
            or hash_fd(final_destination_fd) != result["destination_file_sha256"]
        ):
            raise VerificationError("database changed during verification")
        post_hash_destination_fd, _ = open_regular_at(parent_fd, destination_name, uid, gid)
        opened.append(post_hash_destination_fd)
        if (
            os.fstat(post_hash_destination_fd).st_dev,
            os.fstat(post_hash_destination_fd).st_ino,
        ) != (
            os.fstat(destination_fd).st_dev,
            os.fstat(destination_fd).st_ino,
        ):
            raise VerificationError("migration path changed during verification")
        reject_sidecars(parent_fd, destination_name)
        return result
    finally:
        for descriptor in opened:
            os.close(descriptor)


def verify_conversion(
    source: Path,
    destination: Path,
    manifest_dir: Path,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> dict[str, Any]:
    """Independently verify one descriptor-anchored, fully published conversion."""
    uid = os.getuid() if expected_uid is None else expected_uid
    gid = os.getgid() if expected_gid is None else expected_gid
    parent_fd = source_fd = destination_fd = evidence_fd = None
    try:
        parent, source_name, destination_name, evidence_name = sibling_names(
            source, destination, manifest_dir
        )
        parent_fd = open_directory_chain(parent)
        require_private_directory(parent_fd, uid, gid, "migration parent")
        reject_sidecars(parent_fd, source_name)
        reject_sidecars(parent_fd, destination_name)
        source_fd, source_info = open_regular_at(parent_fd, source_name, uid, gid)
        destination_fd, destination_info = open_regular_at(parent_fd, destination_name, uid, gid)
        if (
            source_info.st_dev != destination_info.st_dev
            or source_info.st_ino == destination_info.st_ino
        ):
            raise VerificationError("source/destination device or inode mismatch")
        evidence_fd = open_private_directory_at(parent_fd, evidence_name, uid, gid)
        result = verify_open_fds(
            source_fd,
            destination_fd,
            evidence_fd,
            uid,
            gid,
            publish_evidence=False,
        )
        return _final_snapshot(
            parent,
            parent_fd,
            source_name,
            destination_name,
            evidence_name,
            source_fd,
            destination_fd,
            evidence_fd,
            uid,
            gid,
            result,
        )
    except VerificationError:
        raise
    except (UnsafeMigrationPath, OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise VerificationError(str(exc)) from exc
    finally:
        for descriptor in (evidence_fd, destination_fd, source_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-tracker-verify-v9-migration")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("manifest_dir", type=Path)
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = verify_conversion(
        args.source,
        args.destination,
        args.manifest_dir,
        expected_uid=args.uid,
        expected_gid=args.gid,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
