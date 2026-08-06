from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

import hermes_tracker.migrate_v8_v9 as converter
import hermes_tracker.verify_v9_migration as verifier
from hermes_tracker.migrate_v8_v9 import ConversionError, convert_database
from hermes_tracker.v8_schema import V8_SCHEMA_SQL
from hermes_tracker.verify_v9_migration import VerificationError, verify_conversion


def _v8_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V8_SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
        [(version, f"v{version}") for version in range(1, 9)],
    )
    conn.execute(
        "INSERT INTO repositories(id,slug,name,description,navigation,published,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            3,
            "legacy",
            "Project",
            "Description",
            json.dumps([{"label": "todos", "url": "/r/legacy"}]),
            1,
            "created",
        ),
    )
    conn.execute(
        "INSERT INTO repository_aliases(alias_slug,repo_id,is_canonical,created_at) "
        "VALUES('project',3,1,'alias-created')"
    )
    statuses = ["open", "active", "blocked", "done"]
    for index, state in enumerate(statuses, 1):
        conn.execute(
            "INSERT INTO todos(id,repo_id,number,title,description,status,priority,assignee,"
            "acceptance_criteria,verification_commands,closing_commit,verification_output,"
            "created_at,updated_at,closed_by,closed_at,author,labels) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                index + 10,
                3,
                index,
                f"Report {state}",
                f"Body {state}",
                state,
                index,
                f"Owner{index}",
                '["criterion"]',
                '["command"]',
                "d" * 40 if state == "done" else None,
                "verified" if state == "done" else None,
                f"created-{index}",
                f"updated-{index}",
                "closer" if state == "done" else None,
                "2026-08-04T12:00:00Z" if state == "done" else None,
                f"author{index}",
                json.dumps(["api", "bug"]),
            ),
        )
    conn.execute(
        "INSERT INTO comments(id,todo_id,author,body,created_at) "
        "VALUES(31,11,'commenter','Body','commented')"
    )
    conn.execute(
        "INSERT INTO todo_links(id,source_todo_id,target_todo_id,link_type,created_at) "
        "VALUES(41,11,12,'related-to','linked')"
    )
    conn.execute(
        "INSERT INTO external_references(id,todo_id,system,external_id,url,metadata,created_at) "
        "VALUES(51,11,'old','1',NULL,'{}','referenced')"
    )
    conn.execute(
        "INSERT INTO superseded_kanban_executions"
        "(todo_id,task_id,rationale,replacement_kanban_id,created_at) "
        "VALUES(11,'t_deadbeef','old',NULL,'superseded')"
    )
    tokens = [
        (61, "keep", '["read","todo:create","todo:close"]', None),
        (62, "retired", '["todo:claim","todo:link"]', None),
        (63, "commit-scanner", '["read"]', None),
    ]
    for token_id, actor, scopes, disabled_at in tokens:
        conn.execute(
            "INSERT INTO agent_tokens"
            "(id,token_prefix,actor,token_hash,scopes,created_at,disabled_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                token_id,
                f"prefix{token_id}",
                actor,
                f"hash{token_id}",
                scopes,
                "issued",
                disabled_at,
            ),
        )
    conn.execute("UPDATE agent_tokens SET last_used_at='last-used' WHERE actor='keep'")
    conn.execute(
        "INSERT INTO tracker_events(id,event_type,todo_id,actor,payload_json,created_at) "
        "VALUES(71,'old',11,'actor','{}','event')"
    )
    conn.execute(
        "INSERT INTO tracker_event_deliveries(event_id,consumer,attempts) VALUES(71,'consumer',2)"
    )
    conn.execute(
        "INSERT INTO mutation_receipts(idempotency_key,operation,request_hash,mutation_id,actor,"
        "status_code,response_json,created_at) "
        "VALUES('old','old','hash','mutation','actor',200,'{}','receipt')"
    )
    conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO comments(id,todo_id,author,body,created_at) VALUES(32,14,'wal','WAL','wal')"
    )
    conn.commit()
    conn.close()
    os.chmod(path, 0o600)


def test_conversion_preserves_retained_data_and_independent_manifests(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    _v8_fixture(source)

    report = convert_database(source, destination, manifests)
    verified = verify_conversion(source, destination, manifests)

    assert report["source_manifest_sha256"] == report["destination_manifest_sha256"]
    assert verified["verified"] is True
    assert destination.stat().st_mode & 0o777 == 0o600
    assert (destination.stat().st_uid, destination.stat().st_gid) == (os.getuid(), os.getgid())
    conn = sqlite3.connect(destination)
    conn.row_factory = sqlite3.Row
    assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations")] == [9]
    assert [row[0] for row in conn.execute("SELECT status FROM todos ORDER BY id")] == [
        "open",
        "open",
        "open",
        "closed",
    ]
    closure = conn.execute("SELECT * FROM todo_closures").fetchone()
    assert closure["commit_sha"] == "d" * 40
    assert closure["source_repo"] == "legacy"
    assert closure["source_ref"] == "refs/heads/legacy-migration"
    assert closure["closed_by"] == "closer"
    assert closure["closed_at"] == "2026-08-04T12:00:00Z"
    assert (
        conn.execute("SELECT verification_output FROM todos WHERE id=14").fetchone()[0]
        == "verified"
    )
    assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 2
    keep = conn.execute("SELECT scopes,disabled_at FROM agent_tokens WHERE actor='keep'").fetchone()
    retired = conn.execute(
        "SELECT scopes,disabled_at FROM agent_tokens WHERE actor='retired'"
    ).fetchone()
    scanner = conn.execute(
        "SELECT scopes,disabled_at FROM agent_tokens WHERE actor='commit-scanner'"
    ).fetchone()
    assert json.loads(keep["scopes"]) == ["read", "todo:create"]
    assert keep["disabled_at"] is None
    assert (
        conn.execute("SELECT last_used_at FROM agent_tokens WHERE actor='keep'").fetchone()[0]
        == "last-used"
    )
    assert json.loads(retired["scopes"]) == [] and retired["disabled_at"]
    assert json.loads(scanner["scopes"]) == ["read"] and scanner["disabled_at"]
    assert dict(
        conn.execute(
            "SELECT name,seq FROM sqlite_sequence "
            "WHERE name IN ('repositories','todos','comments','agent_tokens')"
        )
    ) == {"repositories": 3, "todos": 14, "comments": 32, "agent_tokens": 63}
    assert report["dropped_rows"] == {
        "external_references": 1,
        "mutation_receipts": 1,
        "superseded_kanban_executions": 1,
        "todo_links": 1,
        "tracker_event_deliveries": 1,
        "tracker_events": 1,
    }
    assert report["retained_counts"] == {
        "alias": 1,
        "closure": 1,
        "comment": 2,
        "repository": 1,
        "sequence": 4,
        "todo": 4,
        "token": 3,
    }
    for line in (manifests / "converter-source.jsonl").read_text().splitlines():
        row = json.loads(line)
        digest = row.pop("row_sha256")
        canonical = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        assert digest == hashlib.sha256(canonical.encode()).hexdigest()
    conn.close()


def test_conversion_fails_before_publication_on_unsafe_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _v8_fixture(source)
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    published = False

    def forbidden_publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        published = True
        raise AssertionError("publication must not run")

    monkeypatch.setattr(os, "link", forbidden_publish)

    destination.write_text("occupied")
    with pytest.raises(ConversionError, match="incomplete migration publication"):
        convert_database(source, destination, manifests)
    assert destination.read_text() == "occupied"
    assert not published

    destination.unlink()
    source.chmod(0o644)
    with pytest.raises(ConversionError, match="mode-0600"):
        convert_database(source, destination, manifests)
    assert not destination.exists()
    assert not published

    source.chmod(0o600)
    symlink = tmp_path / "source-link.db"
    symlink.symlink_to(source)
    with pytest.raises(ConversionError):
        convert_database(symlink, destination, manifests)
    assert not destination.exists()
    assert not published


def test_converter_and_verifier_reject_schema_or_manifest_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    _v8_fixture(source)
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE surprise(id INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(ConversionError, match="exact v8 schema"):
        convert_database(source, destination, manifests)

    source.unlink()
    _v8_fixture(source)
    convert_database(source, destination, manifests)
    conn = sqlite3.connect(destination)
    conn.execute("UPDATE todos SET title='tampered' WHERE id=11")
    conn.commit()
    conn.close()
    with pytest.raises(VerificationError, match="manifest mismatch"):
        verify_conversion(source, destination, manifests)


@pytest.mark.parametrize(
    "changed_schema",
    [
        V8_SCHEMA_SQL.replace(
            "'open','active','blocked','done'", "'open','active','blocked','done','mystery'"
        ),
        V8_SCHEMA_SQL.replace(
            "ON repository_aliases(repo_id) WHERE is_canonical=1",
            "ON repository_aliases(alias_slug) WHERE is_canonical=1",
        ),
        V8_SCHEMA_SQL.replace("CHECK(published IN (0,1))", "CHECK(published IN (0,2))"),
    ],
)
def test_converter_rejects_changed_check_and_index_semantics(
    tmp_path: Path, changed_schema: str
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    conn = sqlite3.connect(source)
    conn.executescript(changed_schema)
    conn.executemany(
        "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
        [(version, f"v{version}") for version in range(1, 9)],
    )
    conn.commit()
    conn.close()
    source.chmod(0o600)

    with pytest.raises(ConversionError, match="exact v8 schema"):
        convert_database(source, destination, evidence)
    assert not destination.exists()
    assert not evidence.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda conn: conn.execute("DELETE FROM schema_migrations WHERE version=8"), "exact v8"),
        (lambda conn: conn.execute("UPDATE todos SET labels='not-json' WHERE id=11"), "labels"),
        (
            lambda conn: conn.execute('UPDATE todos SET labels=\'["bug","api"]\' WHERE id=11'),
            "noncanonical labels",
        ),
        (
            lambda conn: conn.execute(
                'UPDATE repositories SET navigation=\'[{"label":"todos","url":"http://bad.example"}]\''
            ),
            "navigation",
        ),
        (lambda conn: conn.execute("CREATE TABLE unexpected(id INTEGER)"), "exact v8"),
    ],
)
def test_converter_rejects_partial_or_malformed_v8(
    tmp_path: Path, mutation: Callable[[sqlite3.Connection], object], message: str
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    _v8_fixture(source)
    conn = sqlite3.connect(source)
    mutation(conn)
    conn.commit()
    conn.close()

    with pytest.raises(ConversionError, match=message):
        convert_database(source, destination, tmp_path / "manifests")
    assert not destination.exists()


def test_converter_rejects_active_sidecars_and_owner_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    _v8_fixture(source)
    sidecar = Path(f"{source}-wal")
    sidecar.touch()

    with pytest.raises(ConversionError, match="sidecars"):
        convert_database(source, destination, tmp_path / "manifests")
    sidecar.unlink()
    with pytest.raises(ConversionError, match="owned mode-0700"):
        convert_database(
            source,
            destination,
            tmp_path / "manifests",
            expected_uid=os.getuid() + 1,
        )
    assert not destination.exists()


def test_converter_rejects_active_transaction(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _v8_fixture(source)
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO comments(id,todo_id,author,body,created_at) "
        "VALUES(99,11,'writer','pending','now')"
    )
    try:
        with pytest.raises(ConversionError, match="sidecars"):
            convert_database(source, tmp_path / "destination.db", tmp_path / "manifests")
    finally:
        writer.rollback()
        writer.close()


def test_converter_detects_source_drift_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    _v8_fixture(source)
    original_write = converter.write_file_at
    writes = 0

    def write_then_drift(directory_fd: int, name: str, content: str, uid: int, gid: int) -> None:
        nonlocal writes
        original_write(directory_fd, name, content, uid, gid)
        writes += 1
        if writes == 3:
            conn = sqlite3.connect(source)
            conn.execute("UPDATE todos SET title='drifted' WHERE id=11")
            conn.commit()
            conn.close()

    monkeypatch.setattr(converter, "write_file_at", write_then_drift)
    with pytest.raises(ConversionError, match="manifest mismatch"):
        convert_database(source, destination, tmp_path / "manifests")
    assert not destination.exists()


def test_converter_cleans_unique_attempt_after_mid_run_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    _v8_fixture(source)

    def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(converter, "write_file_at", fail_manifest)
    with pytest.raises(ConversionError, match="disk full"):
        convert_database(source, destination, tmp_path / "manifests")

    assert not destination.exists()
    assert list(tmp_path.glob("destination.db.attempt-*")) == []


def test_verifier_rejects_database_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    _v8_fixture(source)
    convert_database(source, destination, manifests)
    Path(f"{destination}-journal").touch()

    with pytest.raises(VerificationError, match="sidecars"):
        verify_conversion(source, destination, manifests)
    with pytest.raises(ConversionError, match="sidecars"):
        convert_database(source, destination, manifests)


def test_evidence_is_exclusive_durable_and_replay_recognized(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    _v8_fixture(source)
    source_before = source.read_bytes()
    manifests.mkdir(mode=0o700)
    (manifests / "converter-source.jsonl").symlink_to(source)

    with pytest.raises(ConversionError, match="incomplete migration publication"):
        convert_database(source, destination, manifests)
    assert source.read_bytes() == source_before

    (manifests / "converter-source.jsonl").unlink()
    manifests.rmdir()
    first = convert_database(source, destination, manifests)
    evidence_before = {path.name: path.read_bytes() for path in manifests.iterdir()}
    assert verify_conversion(source, destination, manifests)["verified"] is True
    assert convert_database(source, destination, manifests) == first
    assert evidence_before == {path.name: path.read_bytes() for path in manifests.iterdir()}

    occupied = manifests / "verifier-source.jsonl"
    occupied.write_text("tampered")
    with pytest.raises(VerificationError, match="verifier evidence mismatch"):
        verify_conversion(source, destination, manifests)


def test_evidence_only_crash_state_replays_or_rejects(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    _v8_fixture(source)
    convert_database(source, destination, evidence)

    destination.unlink()
    convert_database(source, destination, evidence)
    assert verify_conversion(source, destination, evidence)["verified"] is True

    destination.unlink()
    (evidence / "converter-report.json").write_text("{}")
    with pytest.raises(ConversionError, match="manifest mismatch"):
        convert_database(source, destination, evidence)
    assert evidence.exists()


def test_invalid_legacy_commit_never_becomes_public_closure(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    _v8_fixture(source)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE todos SET closing_commit='deadbeef' WHERE status='done'")
    conn.commit()
    conn.close()

    convert_database(source, destination, manifests)
    verify_conversion(source, destination, manifests)

    conn = sqlite3.connect(destination)
    assert conn.execute("SELECT status FROM todos WHERE id=14").fetchone()[0] == "closed"
    assert conn.execute("SELECT COUNT(*) FROM todo_closures").fetchone()[0] == 0
    assert (
        conn.execute("SELECT verification_output FROM todos WHERE id=14").fetchone()[0]
        == "verified"
    )
    conn.close()


def test_converter_rejects_non_sibling_and_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    _v8_fixture(source)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)

    with pytest.raises(ConversionError, match="must be siblings"):
        convert_database(source, other / "destination.db", tmp_path / "manifests")

    destination.symlink_to(source)
    with pytest.raises(ConversionError, match="incomplete migration publication"):
        convert_database(source, destination, tmp_path / "manifests")


def test_converter_rejects_symlinked_or_replaced_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    source = actual / "source.db"
    _v8_fixture(source)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ConversionError):
        convert_database(linked / "source.db", linked / "destination.db", linked / "evidence")

    linked.unlink()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    source = state / "source.db"
    _v8_fixture(source)
    moved = tmp_path / "moved"
    redirect = tmp_path / "redirect"
    redirect.mkdir(mode=0o700)
    original_create = converter.create_private_directory_at
    calls = 0

    def replace_parent(directory_fd: int, name: str, uid: int, gid: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            state.rename(moved)
            state.symlink_to(redirect, target_is_directory=True)
        return original_create(directory_fd, name, uid, gid)

    monkeypatch.setattr(converter, "create_private_directory_at", replace_parent)
    with pytest.raises(ConversionError, match="drifted before publication"):
        convert_database(source, state / "destination.db", state / "evidence")
    assert not (moved / "destination.db").exists()
    assert not (redirect / "destination.db").exists()


def test_converter_never_publishes_on_link_failure_or_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    _v8_fixture(source)

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("link failed")

    monkeypatch.setattr(os, "link", fail_link)
    evidence = tmp_path / "link-manifests"
    with pytest.raises(ConversionError, match="link failed"):
        convert_database(source, destination, evidence)
    assert not destination.exists()
    assert evidence.exists()
    assert list(tmp_path.glob("destination.db.attempt-*")) == []

    monkeypatch.undo()
    convert_database(source, destination, evidence)
    assert verify_conversion(source, destination, evidence)["verified"] is True
    original_write = converter.write_file_at
    writes = 0

    def interrupt(directory_fd: int, name: str, content: str, uid: int, gid: int) -> None:
        nonlocal writes
        original_write(directory_fd, name, content, uid, gid)
        writes += 1
        if writes == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(converter, "write_file_at", interrupt)
    interrupted_destination = tmp_path / "interrupted.db"
    evidence = tmp_path / "interrupt-manifests"
    with pytest.raises(KeyboardInterrupt):
        convert_database(source, interrupted_destination, evidence)
    assert not interrupted_destination.exists()
    assert evidence.exists()


def test_converter_recognizes_link_that_completes_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    _v8_fixture(source)

    original_link = os.link

    def complete_then_raise(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)
        raise OSError("link completion uncertain")

    monkeypatch.setattr(os, "link", complete_then_raise)
    report = convert_database(source, destination, evidence)
    assert report["retained_rows"] == 16
    assert verify_conversion(source, destination, evidence)["verified"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "source-mode",
        "destination-mode",
        "evidence-mode",
        "sidecar",
        "destination-swap",
        "evidence-swap",
    ],
)
def test_converter_rejects_post_link_layout_mutation_without_deleting_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    moved = tmp_path / "moved"
    _v8_fixture(source)
    original_link = os.link

    def link_then_mutate(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)
        if mutation == "source-mode":
            source.chmod(0o644)
        elif mutation == "destination-mode":
            destination.chmod(0o644)
        elif mutation == "evidence-mode":
            evidence.chmod(0o755)
        elif mutation == "sidecar":
            Path(f"{destination}-wal").write_text("unrelated")
        elif mutation == "destination-swap":
            destination.rename(moved)
            destination.write_text("unrelated")
            destination.chmod(0o600)
        else:
            evidence.rename(moved)
            evidence.mkdir(mode=0o700)
            (evidence / "unrelated").write_text("preserve")

    monkeypatch.setattr(os, "link", link_then_mutate)
    with pytest.raises(ConversionError, match="published migration layout is unsafe"):
        convert_database(source, destination, evidence)

    if mutation == "destination-swap":
        assert destination.read_text() == "unrelated"
    elif mutation == "evidence-swap":
        assert (evidence / "unrelated").read_text() == "preserve"


def test_converter_removes_publication_after_post_link_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    _v8_fixture(source)
    original_fsync = os.fsync

    def fail_after_link(descriptor: int) -> None:
        if destination.exists() and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_link)
    with pytest.raises(ConversionError, match="directory fsync failed"):
        convert_database(source, destination, evidence)
    assert destination.exists()
    assert evidence.exists()
    monkeypatch.undo()
    assert convert_database(source, destination, evidence)["retained_rows"] == 16


def test_converter_preserves_high_and_empty_sequences(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    conn = sqlite3.connect(source)
    conn.executescript(V8_SCHEMA_SQL)
    conn.executemany(
        "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
        [(version, f"v{version}") for version in range(1, 9)],
    )
    conn.execute(
        "INSERT INTO repositories(id,slug,name,created_at) VALUES(100,'gone','Gone','now')"
    )
    conn.execute(
        "INSERT INTO todos(id,repo_id,number,title,created_at,updated_at) "
        "VALUES(200,100,1,'Gone','now','now')"
    )
    conn.execute(
        "INSERT INTO comments(id,todo_id,author,body,created_at) "
        "VALUES(300,200,'author','gone','now')"
    )
    conn.execute(
        "INSERT INTO agent_tokens(id,token_prefix,actor,token_hash,scopes,created_at) "
        "VALUES(400,'prefix','actor','hash','[]','now')"
    )
    conn.execute("DELETE FROM comments")
    conn.execute("DELETE FROM todos")
    conn.execute("DELETE FROM repositories")
    conn.execute("DELETE FROM agent_tokens")
    before = dict(conn.execute("SELECT name,seq FROM sqlite_sequence"))
    conn.commit()
    conn.close()
    source.chmod(0o600)

    convert_database(source, destination, evidence)
    conn = sqlite3.connect(destination)
    after = dict(conn.execute("SELECT name,seq FROM sqlite_sequence"))
    conn.close()
    assert (
        before
        == after
        == {
            "repositories": 100,
            "todos": 200,
            "comments": 300,
            "agent_tokens": 400,
        }
    )


def test_verifier_rejects_wrong_database_mode(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    manifests = tmp_path / "manifests"
    _v8_fixture(source)
    convert_database(source, destination, manifests)
    destination.chmod(0o640)

    with pytest.raises(VerificationError, match="regular mode-0600"):
        verify_conversion(source, destination, manifests)


def test_verifier_binds_exact_evidence_set_and_database_inode(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    _v8_fixture(source)
    convert_database(source, destination, evidence)

    (evidence / "unexpected").write_text("not evidence")
    with pytest.raises(VerificationError, match="evidence set"):
        verify_conversion(source, destination, evidence)
    (evidence / "unexpected").unlink()

    report_path = evidence / "verifier-report.json"
    report = json.loads(report_path.read_text())
    report["database_inode"]["inode"] += 1
    report_path.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(VerificationError, match="verifier evidence mismatch"):
        verify_conversion(source, destination, evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        "source-content",
        "destination-content",
        "evidence-content",
        "source-mode",
        "destination-mode",
        "evidence-mode",
        "source-swap",
        "destination-swap",
        "evidence-swap",
        "source-sidecar",
        "destination-sidecar",
    ],
)
def test_verifier_rejects_mutation_at_final_validation_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    _v8_fixture(source)
    convert_database(source, destination, evidence)
    original_match = verifier.directory_chain_matches
    moved = tmp_path / f"{mutation}-moved"

    def mutate_then_match(parent: Path, descriptor: int) -> bool:
        matched = original_match(parent, descriptor)
        if mutation in {"source-content", "destination-content"}:
            database = source if mutation == "source-content" else destination
            conn = sqlite3.connect(database)
            conn.execute("UPDATE todos SET title='late mutation' WHERE id=11")
            conn.commit()
            conn.close()
        elif mutation == "evidence-content":
            (evidence / "verifier-source.jsonl").write_text("late mutation")
        elif mutation == "source-mode":
            source.chmod(0o640)
        elif mutation == "destination-mode":
            destination.chmod(0o640)
        elif mutation == "evidence-mode":
            evidence.chmod(0o750)
        elif mutation == "source-swap":
            source.rename(moved)
            source.write_bytes(moved.read_bytes())
            source.chmod(0o600)
        elif mutation == "destination-swap":
            destination.rename(moved)
            destination.write_bytes(moved.read_bytes())
            destination.chmod(0o600)
        elif mutation == "evidence-swap":
            evidence.rename(moved)
            evidence.mkdir(mode=0o700)
        elif mutation == "source-sidecar":
            Path(f"{source}-wal").touch()
        else:
            Path(f"{destination}-wal").touch()
        return matched

    monkeypatch.setattr(verifier, "directory_chain_matches", mutate_then_match)
    with pytest.raises(VerificationError):
        verify_conversion(source, destination, evidence)


def test_verifier_rejects_destination_swap_after_final_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    evidence = tmp_path / "evidence"
    moved = tmp_path / "destination-moved.db"
    _v8_fixture(source)
    convert_database(source, destination, evidence)
    destination_identity = (destination.stat().st_dev, destination.stat().st_ino)
    original_hash = verifier.hash_fd
    destination_hashes = 0

    def hash_then_swap(descriptor: int) -> str:
        nonlocal destination_hashes
        digest = original_hash(descriptor)
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == destination_identity:
            destination_hashes += 1
            if destination_hashes == 2:
                destination.rename(moved)
                destination.write_bytes(moved.read_bytes())
                destination.chmod(0o600)
        return digest

    monkeypatch.setattr(verifier, "hash_fd", hash_then_swap)
    with pytest.raises(VerificationError, match="migration path changed"):
        verify_conversion(source, destination, evidence)
    assert destination_hashes == 2
