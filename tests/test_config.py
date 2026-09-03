"""
Configuration, which is only interesting when it changes.

Both of these were live bugs. `reload()` rebound a module global that every
other module had already imported by value, and the dataclass read the
environment once at class-definition time -- so a config change was invisible
twice over, and the second failure hid the first.
"""
from __future__ import annotations

from prism import config
from prism.config import SETTINGS


def test_reload_is_visible_to_modules_that_imported_settings(monkeypatch):
    from prism import llm
    assert llm.SETTINGS is SETTINGS, "modules must share one Settings instance"
    monkeypatch.setenv("SUNROOM_ENV", "production")
    config.reload()
    try:
        assert llm.SETTINGS.is_production
    finally:
        monkeypatch.setenv("SUNROOM_ENV", "test")
        config.reload()


def test_reload_rereads_the_environment(monkeypatch):
    monkeypatch.setenv("PRISM_MODEL", "claude-test-model")
    config.reload()
    try:
        assert SETTINGS.model == "claude-test-model"
    finally:
        monkeypatch.delenv("PRISM_MODEL", raising=False)
        config.reload()


def test_preflight_is_quiet_outside_production():
    assert SETTINGS.preflight() == []


def test_preflight_catches_a_dangerous_production_config(monkeypatch):
    monkeypatch.setenv("SUNROOM_ENV", "production")
    monkeypatch.setenv("SUNROOM_STORE", "sqlite")
    monkeypatch.setenv("PRISM_PROVIDER", "mock")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUNROOM_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUNROOM_WORKER_SECRET", raising=False)
    config.reload()
    try:
        problems = " ".join(SETTINGS.preflight())
        assert "sqlite" in problems
        assert "Supabase auth" in problems
        assert "SUNROOM_SECRET_KEY" in problems
        assert "SUNROOM_WORKER_SECRET" in problems
        assert "mock" in problems
    finally:
        for k in ("SUNROOM_ENV", "SUNROOM_STORE", "PRISM_PROVIDER"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("SUNROOM_ENV", "test")
        monkeypatch.setenv("SUNROOM_STORE", "sqlite")
        monkeypatch.setenv("PRISM_PROVIDER", "mock")
        monkeypatch.setenv("SUNROOM_SECRET_KEY",
                           "test-secret-key-that-is-long-enough-for-hkdf-0123456789")
        monkeypatch.setenv("SUNROOM_WORKER_SECRET", "test-worker-secret")
        config.reload()
