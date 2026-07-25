"""Tests for application configuration."""

from __future__ import annotations

from hermes_tracker.config import DEFAULT_DB_PATH, Settings


def test_defaults_use_hermes_tracker_names(monkeypatch) -> None:
    for name in (
        "HERMES_TRACKER_DB",
        "HERMES_TRACKER_AUTH_TOKEN",
        "HERMES_TRACKER_ROOT_PATH",
        "HERMES_TRACKER_CGIT_BASE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert DEFAULT_DB_PATH == "hermes_tracker.db"
    assert settings == Settings(
        db_path="hermes_tracker.db",
        auth_token=None,
        root_path="",
    )


def test_settings_read_hermes_tracker_environment(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_TRACKER_DB", "/tmp/hermes-tracker.db")
    monkeypatch.setenv("HERMES_TRACKER_AUTH_TOKEN", "token")
    monkeypatch.setenv("HERMES_TRACKER_ROOT_PATH", "/todos")
    monkeypatch.setenv("HERMES_TRACKER_CGIT_BASE", "https://git.example")

    assert Settings.from_env() == Settings(
        db_path="/tmp/hermes-tracker.db",
        auth_token="token",
        root_path="/todos",
        cgit_base="https://git.example",
    )


def test_legacy_environment_variables_are_ignored(monkeypatch) -> None:
    for name in (
        "HERMES_TRACKER_DB",
        "HERMES_TRACKER_AUTH_TOKEN",
        "HERMES_TRACKER_ROOT_PATH",
        "HERMES_TRACKER_CGIT_BASE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTUM_TODOS_DB", "/tmp/legacy.db")
    monkeypatch.setenv("AGENTUM_TODOS_AUTH_TOKEN", "legacy-token")
    monkeypatch.setenv("AGENTUM_TODOS_ROOT_PATH", "/legacy")
    monkeypatch.setenv("AGENTUM_TODOS_CGIT_BASE", "https://legacy.example")

    assert Settings.from_env() == Settings(
        db_path="hermes_tracker.db",
        auth_token=None,
        root_path="",
        cgit_base="",
    )
