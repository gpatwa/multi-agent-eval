"""Tests for eval_agents.config: candidate credential-skipping, the
all-candidates-missing failure mode, and use-case selection/fallback."""
from __future__ import annotations

import pytest

from eval_agents.config import load_agents, select_use_case
from eval_agents.judge import generic_scorer
from eval_agents.usecases.triage import triage_scorer


def _config(**overrides):
    base = {
        "candidates": [{"name": "mock1", "provider": "mock", "model": "mock-a"}],
        "judge": {"provider": "mock", "model": "mock-judge"},
    }
    base.update(overrides)
    return base


def test_generic_config_defaults_to_generic_scorer():
    system, scorer = select_use_case(_config())
    assert scorer is generic_scorer


def test_triage_use_case_selects_triage_scorer():
    system, scorer = select_use_case(_config(use_case="support_triage"))
    assert scorer is triage_scorer
    assert '"category"' in system  # candidate system prompt carries the JSON contract


def test_unknown_use_case_raises_runtime_error():
    with pytest.raises(RuntimeError, match="Unknown use_case"):
        select_use_case(_config(use_case="not_a_real_use_case"))


def test_load_agents_skips_candidate_missing_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config(
        candidates=[
            {"name": "no_key", "provider": "anthropic", "model": "claude-opus-5"},
            {"name": "mock1", "provider": "mock", "model": "mock-a"},
        ]
    )
    # anthropic has a CLI-profile fallback so it never raises MissingCredentials
    # purely from a missing key in this codebase's registry — use zai instead,
    # which has no such fallback.
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    config["candidates"][0] = {"name": "no_key", "provider": "zai", "model": "glm-5.3"}
    candidates, judge = load_agents(config)
    assert [c.name for c in candidates] == ["mock1"]
    assert judge.name == "judge"


def test_load_agents_raises_when_all_candidates_missing_credentials(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    config = _config(candidates=[{"name": "no_key", "provider": "zai", "model": "glm-5.3"}])
    with pytest.raises(RuntimeError, match="No candidates available"):
        load_agents(config)


def test_load_agents_raises_when_judge_missing_credentials(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    config = _config(judge={"provider": "zai", "model": "glm-5.3"})
    with pytest.raises(RuntimeError, match="Judge unavailable"):
        load_agents(config)
