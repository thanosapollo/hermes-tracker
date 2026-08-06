"""Descriptor-anchored filesystem operations for offline migration."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

EVIDENCE_FILES = (
    "database.db",
    "converter-source.jsonl",
    "converter-destination.jsonl",
    "converter-report.json",
    "verifier-source.jsonl",
    "verifier-destination.jsonl",
    "verifier-report.json",
)
BOUND_EVIDENCE_FILES = tuple(name for name in EVIDENCE_FILES if name != "verifier-report.json")


class UnsafeMigrationPath(RuntimeError):
    pass


def list_directory_fd(directory_fd: int) -> set[str]:
    """Return directory entries via procfd so openat-dir streams stay reliable."""
    return set(os.listdir(f"/proc/self/fd/{directory_fd}"))


def sibling_names(source: Path, destination: Path, evidence: Path) -> tuple[Path, str, str, str]:
    """Return one lexical parent and leaf names without resolving symlinks."""
    absolute = tuple(Path(os.path.abspath(path)) for path in (source, destination, evidence))
    if any(path.name in {"", ".", ".."} for path in absolute):
        raise UnsafeMigrationPath("migration paths must name direct children")
    parents = {path.parent for path in absolute}
    if len(parents) != 1:
        raise UnsafeMigrationPath("source, destination, and evidence must be siblings")
    if len(set(absolute)) != 3:
        raise UnsafeMigrationPath("migration paths collide")
    return absolute[0].parent, absolute[0].name, absolute[1].name, absolute[2].name


def open_directory_chain(path: Path) -> int:
    """Open every absolute directory component without following symlinks."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def directory_chain_matches(path: Path, descriptor: int) -> bool:
    """Return whether the lexical path still names the anchored directory."""
    try:
        current = open_directory_chain(path)
    except OSError:
        return False
    try:
        left = os.fstat(descriptor)
        right = os.fstat(current)
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    finally:
        os.close(current)


def require_private_directory(descriptor: int, uid: int, gid: int, label: str) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (info.st_uid, info.st_gid) != (uid, gid)
    ):
        raise UnsafeMigrationPath(f"{label} must be an owned mode-0700 directory")


def exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def open_regular_at(directory_fd: int, name: str, uid: int, gid: int) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise UnsafeMigrationPath(f"{name} does not exist") from exc
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or (info.st_uid, info.st_gid) != (uid, gid)
    ):
        os.close(descriptor)
        raise UnsafeMigrationPath(f"{name} must be an owned regular mode-0600 file")
    return descriptor, info


def open_private_directory_at(directory_fd: int, name: str, uid: int, gid: int) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        require_private_directory(descriptor, uid, gid, name)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def create_private_directory_at(directory_fd: int, name: str, uid: int, gid: int) -> int:
    os.mkdir(name, 0o700, dir_fd=directory_fd)
    descriptor = open_private_directory_at(directory_fd, name, os.getuid(), os.getgid())
    try:
        os.fchown(descriptor, uid, gid)
        require_private_directory(descriptor, uid, gid, name)
        os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def write_file_at(directory_fd: int, name: str, content: str, uid: int, gid: int) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(content)
            stream.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_file_at(directory_fd: int, name: str, uid: int, gid: int) -> str:
    descriptor, _ = open_regular_at(directory_fd, name, uid, gid)
    try:
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def hash_fd(descriptor: int) -> str:
    duplicate = os.dup(descriptor)
    os.lseek(duplicate, 0, os.SEEK_SET)
    with os.fdopen(duplicate, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evidence_binding(
    evidence_fd: int,
    database_fd: int,
    uid: int,
    gid: int,
    *,
    exact_names: tuple[str, ...],
) -> dict[str, object]:
    """Bind exact evidence members to content and anchored filesystem identities."""
    # List via procfd. Direct os.listdir(dir_fd) can return [] after openat writes
    # on some kernels/Python builds while path-based listing is complete.
    if list_directory_fd(evidence_fd) != set(exact_names):
        raise UnsafeMigrationPath("migration evidence set is not exact")
    evidence_info = os.fstat(evidence_fd)
    database_info = os.fstat(database_fd)
    hashes: dict[str, str] = {}
    for name in BOUND_EVIDENCE_FILES:
        descriptor, _ = open_regular_at(evidence_fd, name, uid, gid)
        try:
            hashes[name] = hash_fd(descriptor)
        finally:
            os.close(descriptor)
    return {
        "database_inode": {"device": database_info.st_dev, "inode": database_info.st_ino},
        "evidence_directory_inode": {
            "device": evidence_info.st_dev,
            "inode": evidence_info.st_ino,
        },
        "evidence_sha256": hashes,
    }


def reject_sidecars(directory_fd: int, name: str) -> None:
    if any(exists_at(directory_fd, f"{name}{suffix}") for suffix in ("-wal", "-shm", "-journal")):
        raise UnsafeMigrationPath(f"{name} has active SQLite sidecars; checkpoint while stopped")
