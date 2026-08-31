"""Tests for scripts/provider_status.py's classification logic (env-var and
mock/CLI-absent branches only -- no live network calls or subprocess spawns
in the test suite)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "provider_status.py"
spec = importlib.util.spec_from_file_location("provider_status", SCRIPT)
provider_status = importlib.util.module_from_spec(spec)
sys.modules["provider_status"] = provider_status
spec.loader.exec_module(provider_status)


def test_mock_is_always_ready():
    status, _ = provider_status.check_one("mock", live=False)
    assert status == "ready"


def test_api_key_provider_pending_without_env_var(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    status, detail = provider_status.check_one("zai", live=False)
    assert status == "pending"
    assert "ZAI_API_KEY" in detail


def test_api_key_provider_ready_with_env_var(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    status, _ = provider_status.check_one("zai", live=False)
    assert status == "ready"


def test_anthropic_is_optimistic_without_key(monkeypatch):
    """Anthropic has an ant-auth-login fallback, so a missing env var isn't
    a hard 'pending' the way it is for every other API-key provider."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status, detail = provider_status.check_one("anthropic", live=False)
    assert status == "ready?"
    assert "ant auth login" in detail


def test_cli_provider_pending_when_binary_missing(monkeypatch):
    monkeypatch.setattr(provider_status.shutil, "which", lambda _: None)
    status, detail = provider_status.check_one("claude-code", live=False)
    assert status == "pending"
    assert "not installed" in detail


def test_cli_provider_optimistic_when_installed_and_not_live(monkeypatch):
    monkeypatch.setattr(provider_status.shutil, "which", lambda _: "/usr/local/bin/claude")
    status, detail = provider_status.check_one("claude-code", live=False)
    assert status == "ready?"
    assert "--live" in detail
