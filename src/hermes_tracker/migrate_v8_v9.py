"""Fail-closed, out-of-place SQLite v8-to-v9 converter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import urllib.parse
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .db import SCHEMA_SQL, validate_schema
from .domain import normalize_labels
from .migration_io import (
    EVIDENCE_FILES,
    UnsafeMigrationPath,
    create_private_directory_at,
    directory_chain_matches,
    exists_at,
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
from .timeutil import utc_now
from .v8_schema import EXPECTED_V8_FINGERPRINT, structural_fingerprint

RETAINED_SCOPES = frozenset({"read", "repo:manage", "todo:create", "todo:update", "comment:create"})
DROPPED_TABLES = (
    "external_references",
    "mutation_receipts",
    "superseded_kanban_executions",
    "todo_links",
    "tracker_event_deliveries",
    "tracker_events",
)
NAVIGATION_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,49}$")
FULL_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
RETAINED_SEQUENCES = ("repositories", "todos", "comments", "agent_tokens")


class ConversionError(RuntimeError):
    pass


def _json_list(raw: str, *, field: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"malformed {field}") from exc
    if not isinstance(value, list):
        raise ConversionError(f"malformed {field}")
    return value


def _navigation(raw: str) -> list[dict[str, str]]:
    value = _json_list(raw, field="navigation")
    if any(
        not isinstance(item, dict)
        or set(item) != {"label", "url"}
        or not isinstance(item["label"], str)
        or not isinstance(item["url"], str)
        for item in value
    ):
        raise ConversionError("malformed navigation")
    labels: set[str] = set()
    for item in value:
        label, url = item["label"], item["url"]
        try:
            parsed = urllib.parse.urlsplit(url)
            _ = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise ConversionError("malformed navigation") from exc
        valid_url = (
            url.startswith("/")
            and not url.startswith("//")
            and not parsed.query
            and not parsed.fragment
        ) or (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
        if (
            not NAVIGATION_LABEL.fullmatch(label)
            or label in labels
            or len(url) > 2000
            or any(ord(character) < 32 for character in url)
            or not valid_url
        ):
            raise ConversionError("malformed navigation")
        labels.add(label)
    if len(value) > 50:
        raise ConversionError("malformed navigation")
    return cast(list[dict[str, str]], value)


def _labels(raw: str) -> list[str]:
    value = _json_list(raw, field="labels")
    if any(not isinstance(item, str) for item in value):
        raise ConversionError("malformed labels")
    normalized = normalize_labels(value)
    if value != normalized:
        raise ConversionError("noncanonical labels")
    return normalized


def _scopes(raw: str) -> list[str]:
    value = _json_list(raw, field="token scopes")
    if any(not isinstance(item, str) for item in value):
        raise ConversionError("malformed token scopes")
    return sorted(set(value) & RETAINED_SCOPES)


def _destination_scopes(raw: str) -> list[str]:
    value = _json_list(raw, field="token scopes")
    if any(not isinstance(item, str) for item in value):
        raise ConversionError("malformed token scopes")
    if value != sorted(set(value)) or any(item not in RETAINED_SCOPES for item in value):
        raise ConversionError("noncanonical destination token scopes")
    return cast(list[str], value)


def _canonical_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _valid_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _manifest(rows: list[dict[str, Any]]) -> tuple[str, str]:
    lines = []
    for row in rows:
        digest = hashlib.sha256(_canonical_line(row).encode()).hexdigest()
        lines.append(f"{_canonical_line({**row, 'row_sha256': digest})}\n")
    content = "".join(lines)
    return content, hashlib.sha256(content.encode()).hexdigest()


def _project_source(conn: sqlite3.Connection, disabled_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    repo_slugs: dict[int, str] = {}
    for row in conn.execute("SELECT * FROM repositories ORDER BY id"):
        repo_slugs[row["id"]] = row["slug"]
        rows.append(
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
    for row in conn.execute("SELECT * FROM repository_aliases ORDER BY alias_slug"):
        rows.append({"kind": "alias", **dict(row)})
    closures: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM todos ORDER BY repo_id,number"):
        if row["status"] not in {"open", "active", "blocked", "done"}:
            raise ConversionError("malformed todo status")
        status = "closed" if row["status"] == "done" else "open"
        rows.append(
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
            closures.append(
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
    closures.sort(key=lambda item: item["todo_id"])
    rows.extend(closures)
    for row in conn.execute("SELECT * FROM comments ORDER BY id"):
        rows.append({"kind": "comment", **dict(row)})
    for row in conn.execute("SELECT * FROM agent_tokens ORDER BY id"):
        scopes = _scopes(row["scopes"])
        must_disable = not scopes or row["actor"] == "commit-scanner"
        rows.append(
            {
                "kind": "token",
                "id": row["id"],
                "token_prefix": row["token_prefix"],
                "actor": row["actor"],
                "token_hash": row["token_hash"],
                "scopes": scopes,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "disabled_at": row["disabled_at"] or (disabled_at if must_disable else None),
                "last_used_at": row["last_used_at"],
            }
        )
    rows.extend(_sequence_rows(conn))
    return rows


def _project_destination(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM repositories ORDER BY id"):
        rows.append(
            {"kind": "repository", **dict(row), "navigation": _navigation(row["navigation"])}
        )
    for row in conn.execute("SELECT * FROM repository_aliases ORDER BY alias_slug"):
        rows.append({"kind": "alias", **dict(row)})
    for row in conn.execute("SELECT * FROM todos ORDER BY repo_id,number"):
        rows.append({"kind": "todo", **dict(row), "labels": _labels(row["labels"])})
    for row in conn.execute("SELECT * FROM todo_closures ORDER BY todo_id"):
        rows.append({"kind": "closure", **dict(row)})
    for row in conn.execute("SELECT * FROM comments ORDER BY id"):
        rows.append({"kind": "comment", **dict(row)})
    for row in conn.execute("SELECT * FROM agent_tokens ORDER BY id"):
        rows.append({"kind": "token", **dict(row), "scopes": _destination_scopes(row["scopes"])})
    rows.extend(_sequence_rows(conn))
    return rows


def _sequence_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for table in RETAINED_SEQUENCES:
        maximum = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        if row is None:
            if maximum is not None:
                raise ConversionError(f"missing sqlite_sequence for {table}")
            continue
        sequence = row[0]
        if (
            not isinstance(sequence, int)
            or sequence < 0
            or (maximum is not None and sequence < maximum)
        ):
            raise ConversionError(f"invalid sqlite_sequence for {table}")
        result.append({"kind": "sequence", "table": table, "value": sequence})
    return result


def _validate_source(conn: sqlite3.Connection) -> None:
    try:
        versions = [
            row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        valid = (
            versions == list(range(1, 9))
            and structural_fingerprint(conn) == EXPECTED_V8_FINGERPRINT
            and conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            and not conn.execute("PRAGMA foreign_key_check").fetchall()
        )
    except sqlite3.DatabaseError as exc:
        raise ConversionError("source is not exact v8 schema") from exc
    if not valid:
        raise ConversionError("source is not exact v8 schema")


def _set_sequences(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.execute(
        f"DELETE FROM sqlite_sequence WHERE name IN ({','.join('?' for _ in RETAINED_SEQUENCES)})",
        RETAINED_SEQUENCES,
    )
    for row in rows:
        if row["kind"] == "sequence":
            conn.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",
                (row["table"], row["value"]),
            )


def _source_unchanged(
    parent_fd: int,
    source_name: str,
    source_fd: int,
    source_info: os.stat_result,
    source_hash: str,
    uid: int,
    gid: int,
) -> bool:
    current = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
    anchored = os.fstat(source_fd)
    reject_sidecars(parent_fd, source_name)
    required_mode = 0o600
    return (
        stat.S_ISREG(current.st_mode)
        and stat.S_ISREG(anchored.st_mode)
        and stat.S_IMODE(current.st_mode) == required_mode
        and stat.S_IMODE(anchored.st_mode) == required_mode
        and current.st_uid == anchored.st_uid == uid
        and current.st_gid == anchored.st_gid == gid
        and (current.st_dev, current.st_ino, current.st_size)
        == (anchored.st_dev, anchored.st_ino, anchored.st_size)
        == (source_info.st_dev, source_info.st_ino, source_info.st_size)
        and hash_fd(source_fd) == source_hash
    )


def _published_layout_is_exact(
    parent: Path,
    parent_fd: int,
    source_name: str,
    destination_name: str,
    evidence_name: str,
    source_fd: int,
    database_fd: int,
    evidence_fd: int,
    source_info: os.stat_result,
    source_hash: str,
    uid: int,
    gid: int,
) -> bool:
    opened: list[int] = []
    try:
        if not directory_chain_matches(parent, parent_fd):
            return False
        if not _source_unchanged(
            parent_fd, source_name, source_fd, source_info, source_hash, uid, gid
        ):
            return False
        reject_sidecars(parent_fd, destination_name)
        destination_fd, _ = open_regular_at(parent_fd, destination_name, uid, gid)
        opened.append(destination_fd)
        published_evidence_fd = open_private_directory_at(parent_fd, evidence_name, uid, gid)
        opened.append(published_evidence_fd)
        bundled_database_fd, _ = open_regular_at(published_evidence_fd, "database.db", uid, gid)
        opened.append(bundled_database_fd)
        expected_database = os.fstat(database_fd)
        expected_evidence = os.fstat(evidence_fd)
        return (
            (os.fstat(destination_fd).st_dev, os.fstat(destination_fd).st_ino)
            == (expected_database.st_dev, expected_database.st_ino)
            == (os.fstat(bundled_database_fd).st_dev, os.fstat(bundled_database_fd).st_ino)
            and (os.fstat(published_evidence_fd).st_dev, os.fstat(published_evidence_fd).st_ino)
            == (expected_evidence.st_dev, expected_evidence.st_ino)
            and set(os.listdir(published_evidence_fd)) == set(EVIDENCE_FILES)
        )
    except (OSError, UnsafeMigrationPath):
        return False
    finally:
        for descriptor in opened:
            os.close(descriptor)


def _publish_database(
    parent: Path,
    parent_fd: int,
    source_name: str,
    destination_name: str,
    evidence_name: str,
    source_fd: int,
    database_fd: int,
    evidence_fd: int,
    source_info: os.stat_result,
    source_hash: str,
    uid: int,
    gid: int,
) -> None:
    try:
        os.link(
            "database.db",
            destination_name,
            src_dir_fd=evidence_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        if not _published_layout_is_exact(
            parent,
            parent_fd,
            source_name,
            destination_name,
            evidence_name,
            source_fd,
            database_fd,
            evidence_fd,
            source_info,
            source_hash,
            uid,
            gid,
        ):
            raise
    os.fsync(parent_fd)


def convert_database(
    source: Path,
    destination: Path,
    manifest_dir: Path,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> dict[str, Any]:
    """Convert exact v8 source into one evidence-bound v9 publication."""
    from .verify_v9_migration import VerificationError, verify_open_fds

    uid = os.getuid() if expected_uid is None else expected_uid
    gid = os.getgid() if expected_gid is None else expected_gid
    parent_fd = source_fd = evidence_fd = database_fd = None
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        parent, source_name, destination_name, evidence_name = sibling_names(
            source, destination, manifest_dir
        )
        parent_fd = open_directory_chain(parent)
        require_private_directory(parent_fd, uid, gid, "migration parent")
        reject_sidecars(parent_fd, source_name)
        source_fd, source_info = open_regular_at(parent_fd, source_name, uid, gid)
        source_file_hash = hash_fd(source_fd)

        destination_exists = exists_at(parent_fd, destination_name)
        evidence_exists = exists_at(parent_fd, evidence_name)
        if destination_exists and not evidence_exists:
            raise ConversionError("incomplete migration publication exists")
        if evidence_exists:
            if destination_exists:
                reject_sidecars(parent_fd, destination_name)
            evidence_fd = open_private_directory_at(parent_fd, evidence_name, uid, gid)
            if set(os.listdir(evidence_fd)) != set(EVIDENCE_FILES):
                raise ConversionError("incomplete migration publication exists")
            database_fd, destination_info = open_regular_at(evidence_fd, "database.db", uid, gid)
            if source_info.st_dev != destination_info.st_dev:
                raise ConversionError("destination is on a different filesystem")
            verify_open_fds(
                source_fd,
                database_fd,
                evidence_fd,
                uid,
                gid,
                publish_evidence=False,
            )
            report = json.loads(read_file_at(evidence_fd, "converter-report.json", uid, gid))
            if not destination_exists:
                _publish_database(
                    parent,
                    parent_fd,
                    source_name,
                    destination_name,
                    evidence_name,
                    source_fd,
                    database_fd,
                    evidence_fd,
                    source_info,
                    source_file_hash,
                    uid,
                    gid,
                )
            if not _published_layout_is_exact(
                parent,
                parent_fd,
                source_name,
                destination_name,
                evidence_name,
                source_fd,
                database_fd,
                evidence_fd,
                source_info,
                source_file_hash,
                uid,
                gid,
            ):
                raise ConversionError("published migration layout is unsafe")
            verify_open_fds(
                source_fd,
                database_fd,
                evidence_fd,
                uid,
                gid,
                publish_evidence=False,
            )
            return report

        source_conn = sqlite3.connect(
            f"file:/proc/self/fd/{source_fd}?mode=ro&immutable=1", uri=True
        )
        source_conn.row_factory = sqlite3.Row
        source_conn.execute("PRAGMA foreign_keys=ON")
        _validate_source(source_conn)
        disabled_at = utc_now()
        source_rows = _project_source(source_conn, disabled_at)
        source_content, source_hash = _manifest(source_rows)

        evidence_fd = create_private_directory_at(parent_fd, evidence_name, uid, gid)
        descriptor = os.open(
            "database.db",
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=evidence_fd,
        )
        os.fchown(descriptor, uid, gid)
        os.close(descriptor)

        destination_conn = sqlite3.connect(f"/proc/self/fd/{evidence_fd}/database.db")
        destination_conn.row_factory = sqlite3.Row
        destination_conn.execute("PRAGMA foreign_keys=ON")
        destination_conn.executescript(SCHEMA_SQL)
        destination_conn.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(9,?)", (disabled_at,)
        )
        for row in source_rows:
            kind = row["kind"]
            if kind == "sequence":
                continue
            values = {key: value for key, value in row.items() if key != "kind"}
            if kind in {"repository", "todo", "token"}:
                for field in ("navigation", "labels", "scopes"):
                    if field in values:
                        values[field] = json.dumps(
                            values[field], ensure_ascii=False, separators=(",", ":")
                        )
            table = {
                "repository": "repositories",
                "alias": "repository_aliases",
                "todo": "todos",
                "closure": "todo_closures",
                "comment": "comments",
                "token": "agent_tokens",
            }[kind]
            columns = list(values)
            statement = (
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})"
            )
            destination_conn.execute(statement, [values[column] for column in columns])
        _set_sequences(destination_conn, source_rows)
        destination_conn.commit()
        validate_schema(destination_conn)
        destination_rows = _project_destination(destination_conn)
        destination_content, destination_hash = _manifest(destination_rows)
        if source_hash != destination_hash:
            raise ConversionError("source and destination manifests differ")
        dropped_rows = {
            table: source_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in DROPPED_TABLES
        }
        destination_conn.close()
        destination_conn = None
        database_fd, _ = open_regular_at(evidence_fd, "database.db", uid, gid)
        os.fsync(database_fd)
        report = {
            "migration_applied_at": disabled_at,
            "source_manifest_sha256": source_hash,
            "destination_manifest_sha256": destination_hash,
            "source_file_sha256": source_file_hash,
            "destination_file_sha256": hash_fd(database_fd),
            "retained_rows": len(source_rows),
            "retained_counts": dict(sorted(Counter(row["kind"] for row in source_rows).items())),
            "dropped_rows": dropped_rows,
        }
        for name, content in {
            "converter-source.jsonl": source_content,
            "converter-destination.jsonl": destination_content,
            "converter-report.json": json.dumps(report, sort_keys=True, separators=(",", ":"))
            + "\n",
        }.items():
            write_file_at(evidence_fd, name, content, uid, gid)
        os.fsync(evidence_fd)
        verify_open_fds(
            source_fd,
            database_fd,
            evidence_fd,
            uid,
            gid,
            publish_evidence=True,
        )
        if not _source_unchanged(
            parent_fd,
            source_name,
            source_fd,
            source_info,
            source_file_hash,
            uid,
            gid,
        ) or not directory_chain_matches(parent, parent_fd):
            raise ConversionError("source drifted before publication")
        _publish_database(
            parent,
            parent_fd,
            source_name,
            destination_name,
            evidence_name,
            source_fd,
            database_fd,
            evidence_fd,
            source_info,
            source_file_hash,
            uid,
            gid,
        )
        if not _published_layout_is_exact(
            parent,
            parent_fd,
            source_name,
            destination_name,
            evidence_name,
            source_fd,
            database_fd,
            evidence_fd,
            source_info,
            source_file_hash,
            uid,
            gid,
        ):
            raise ConversionError("published migration layout is unsafe")
        verify_open_fds(
            source_fd,
            database_fd,
            evidence_fd,
            uid,
            gid,
            publish_evidence=False,
        )
        return report
    except BaseException as exc:
        if isinstance(exc, ConversionError | KeyboardInterrupt | SystemExit):
            raise
        if isinstance(exc, VerificationError):
            raise ConversionError(str(exc)) from exc
        if isinstance(exc, UnsafeMigrationPath | OSError | sqlite3.DatabaseError | ValueError):
            raise ConversionError(str(exc)) from exc
        raise
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()
        for open_descriptor in (database_fd, evidence_fd, source_fd, parent_fd):
            if open_descriptor is not None:
                os.close(open_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-tracker-migrate-v8-v9")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("manifest_dir", type=Path)
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--gid", type=int, default=os.getgid())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = convert_database(
        args.source,
        args.destination,
        args.manifest_dir,
        expected_uid=args.uid,
        expected_gid=args.gid,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
