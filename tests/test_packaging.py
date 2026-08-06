"""Regression tests for source-distribution contents."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _copy_release_fixture(destination: Path) -> None:
    """Copy the release inputs needed to exercise Hatch in an isolated project."""
    for filename in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        shutil.copy2(PROJECT_ROOT / filename, destination / filename)
    for directory in ("src", "docs"):
        shutil.copytree(
            PROJECT_ROOT / directory,
            destination / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )


def test_sdist_excludes_private_worktrees_and_keeps_release_assets(tmp_path: Path) -> None:
    project = tmp_path / "package-fixture"
    project.mkdir()
    _copy_release_fixture(project)
    (project / ".worktrees" / "private" / "nested").mkdir(parents=True)
    (project / ".worktrees" / "private" / "nested" / "sentinel.py").write_text(
        "private worktree content\n"
    )
    (project / ".hermes" / "plans").mkdir(parents=True)
    (project / ".hermes" / "plans" / "sentinel.md").write_text("private plan\n")
    (project / "tracker.sqlite").write_text("local database\n")
    output = tmp_path / "dist"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--outdir",
            str(output),
            str(project),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    archive = next(output.glob("*.tar.gz"))
    with tarfile.open(archive) as distribution:
        members = distribution.getnames()

    member_paths = [Path(member) for member in members]
    assert all(".worktrees" not in path.parts for path in member_paths)
    assert all(".hermes" not in path.parts for path in member_paths)
    assert all(not member.endswith("tracker.sqlite") for member in members)
    assert all("agentum_todos" not in path.parts for path in member_paths)
    release_root = next(path.parts[0] for path in member_paths if path.parts)
    assert {
        f"{release_root}/pyproject.toml",
        f"{release_root}/README.md",
        f"{release_root}/LICENSE",
        f"{release_root}/CONTRIBUTING.md",
        f"{release_root}/SECURITY.md",
        f"{release_root}/src/hermes_tracker/app.py",
        f"{release_root}/src/hermes_tracker/commit_directives.py",
        f"{release_root}/src/hermes_tracker/db.py",
        f"{release_root}/src/hermes_tracker/migrate_v8_v9.py",
        f"{release_root}/src/hermes_tracker/migration_io.py",
        f"{release_root}/src/hermes_tracker/verify_v9_migration.py",
        f"{release_root}/src/hermes_tracker/web/templates/base.html",
        f"{release_root}/src/hermes_tracker/web/static/tracker.css",
        f"{release_root}/src/hermes_tracker/web/static/filter-complete.js",
        f"{release_root}/docs/deployment.md",
    } <= set(members)


def test_deployment_document_uses_only_canonical_runtime_identity() -> None:
    deployment = (PROJECT_ROOT / "docs" / "deployment.md").read_text()

    assert "agentum" not in deployment.lower()


def test_project_excludes_removed_scanner_and_mcp_commands() -> None:
    project = (PROJECT_ROOT / "pyproject.toml").read_text()

    assert "commit_scanner" not in project
    assert "hermes-tracker-mcp" not in project
    assert 'hermes-tracker-migrate-v8-v9 = "hermes_tracker.migrate_v8_v9:main"' in project
    assert (
        'hermes-tracker-verify-v9-migration = "hermes_tracker.verify_v9_migration:main"' in project
    )
