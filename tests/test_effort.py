"""Matched reasoning effort: config resolution, adapter wire formats, loud
failure for adapters that can't apply it, and what gets recorded."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from eval_agents.agents import Agent
from eval_agents.config import describe_run, load_agents, select_effort
from eval_agents.providers import cli_providers
from eval_agents.providers.anthropic_provider import AnthropicProvider
from eval_agents.providers.base import ChatMessage, Provider
from eval_agents.providers.cli_providers import ClaudeCodeProvider, CodexProvider, GeminiCliProvider
from eval_agents.providers.gemini_provider import GeminiProvider
from eval_agents.providers.mock_provider import MockProvider
from eval_agents.providers.openai_provider import OpenAIProvider
from eval_agents.providers.zai_provider import ZaiProvider
from eval_agents.registry import create_provider
from eval_agents.report import to_markdown, to_summary_json
from eval_agents.runner import CandidateResult, Task, TaskResult

MSGS = [ChatMessage(role="user", content="hi")]


# ---------------------------------------------------------------- config resolution


def test_select_effort_precedence_and_validation():
    assert select_effort({}, {}) is None
    assert select_effort({"effort": "low"}, {}) == "low"                       # top-level default
    assert select_effort({"effort": "low"}, {"effort": "high"}) == "high"      # per-candidate wins
    assert select_effort({"effort": "low"}, {}, inherit=False) is None         # judges don't inherit
    assert select_effort({}, {"effort": "medium"}, inherit=False) == "medium"
    with pytest.raises(RuntimeError, match="effort"):
        select_effort({"effort": "turbo"}, {})


def _config(**extra):
    return {
        "candidates": [{"name": "a", "provider": "mock", "model": "m1"}, {"name": "b", "provider": "mock", "model": "m2"}],
        "judge": {"provider": "mock", "model": "mock-judge"}, **extra,
    }


def test_load_agents_applies_top_level_effort_to_candidates_not_judge():
    candidates, judge = load_agents(_config(effort="low"))
    assert [c.provider.effort for c in candidates] == ["low", "low"]
    assert judge.provider.effort is None
    config = _config(effort="low")
    config["judge"]["effort"] = "high"  # a judge only gets an effort when it names one itself
    assert load_agents(config)[1].provider.effort == "high"


def test_effort_the_adapter_cannot_apply_is_an_error_not_silently_ignored():
    config = _config(effort="low")
    config["candidates"][1] = {"name": "b", "provider": "gemini-cli", "model": "default"}
    with pytest.raises(RuntimeError, match="candidate 'b'.*can't set reasoning effort"):
        load_agents(config)


def test_openai_compatible_subclasses_cannot_promise_effort():
    assert OpenAIProvider.supported_efforts.fget(OpenAIProvider.__new__(OpenAIProvider))  # first-party: yes
    assert ZaiProvider.__new__(ZaiProvider).supported_efforts is None
    with pytest.raises(ValueError, match="can't set reasoning effort"):
        ZaiProvider.__new__(ZaiProvider).set_effort("low")


def test_unsupported_level_for_an_adapter_names_the_valid_ones():
    with pytest.raises(ValueError, match=r"\['low', 'medium', 'high'\]"):
        GeminiProvider.__new__(GeminiProvider).set_effort("max")
    assert create_provider("mock", "m", effort="max").effort == "max"  # registry passes it through


# ---------------------------------------------------------------- API adapters (stub clients)


def test_anthropic_sends_output_config_effort_only_when_set():
    sent = []

    def create(**kw):
        sent.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], model="m",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    p = AnthropicProvider.__new__(AnthropicProvider)
    p.model, p.client = "claude-opus-5-5", SimpleNamespace(messages=SimpleNamespace(create=create))
    p.complete(MSGS)
    p.set_effort("low")
    p.complete(MSGS)
    assert "output_config" not in sent[0] and sent[1]["output_config"] == {"effort": "low"}


def test_openai_sends_reasoning_effort_only_when_set():
    sent = []

    def create(**kw):
        sent.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
                               model="m", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    p = OpenAIProvider.__new__(OpenAIProvider)
    p.model, p.client = "gpt-6-astra", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    p.complete(MSGS)
    p.set_effort("low")
    p.complete(MSGS)
    assert "reasoning_effort" not in sent[0] and sent[1]["reasoning_effort"] == "low"


def test_gemini_uses_thinking_level_instead_of_budget_when_effort_set():
    from google.genai import types

    sent = []

    def generate_content(**kw):
        sent.append(kw["config"].thinking_config)
        return SimpleNamespace(text="ok", candidates=[], usage_metadata=None)

    p = GeminiProvider.__new__(GeminiProvider)
    p.model, p.client = "gemini-3.1-pro-preview", SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    p.complete(MSGS, max_tokens=4096)
    p.set_effort("low")
    p.complete(MSGS, max_tokens=4096)
    assert sent[0].thinking_budget == 1024 and sent[0].thinking_level is None      # historical behaviour
    assert sent[1].thinking_level == types.ThinkingLevel.LOW and sent[1].thinking_budget is None


# ---------------------------------------------------------------- CLI adapters


class _Proc:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


def _fake_cli(monkeypatch, provider_cls, model, effort):
    calls = []
    monkeypatch.setattr(cli_providers.shutil, "which", lambda b: f"/bin/{b}")
    monkeypatch.setattr(cli_providers.os.path, "exists", lambda p: True)
    p = provider_cls(model)
    p.set_effort(effort)
    monkeypatch.setattr(p, "_run", lambda args, stdin=None: calls.append(args) or _Proc('{"result": "ok", "usage": {}}'))
    return p, calls


def test_claude_code_passes_effort_flag(monkeypatch):
    p, calls = _fake_cli(monkeypatch, ClaudeCodeProvider, "claude-opus-5", "low")
    p.complete(MSGS)
    args = calls[0]
    assert args[args.index("--effort") + 1] == "low" and args[args.index("--model") + 1] == "claude-opus-5"
    p2, calls2 = _fake_cli(monkeypatch, ClaudeCodeProvider, "claude-opus-5", None)
    p2.complete(MSGS)
    assert "--effort" not in calls2[0]


def test_codex_passes_effort_as_config_override_and_records_personal_config(monkeypatch, tmp_path):
    (tmp_path / "config.toml").write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\nservice_tier = "priority"\n\n[plugins.x]\nservice_tier = "ignored"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    p, calls = _fake_cli(monkeypatch, CodexProvider, "gpt-6-astra", "low")
    monkeypatch.setattr(p, "_run", lambda args, stdin=None: calls.append(args))
    monkeypatch.setattr(cli_providers.tempfile, "NamedTemporaryFile", lambda *a, **k: open(tmp_path / "out.md", "w+"))
    monkeypatch.setattr(cli_providers.os, "unlink", lambda p: None)
    p.complete(MSGS)
    args = calls[0]
    assert args[args.index("-c") + 1] == 'model_reasoning_effort="low"' and args[args.index("--model") + 1] == "gpt-6-astra"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="codex-cli 9.9\n"))
    info = p.describe()
    assert info["effort"] == "low" and info["cli_version"] == "codex-cli 9.9"
    assert info["user_config"] == {"model": "gpt-6-astra", "model_reasoning_effort": "high", "service_tier": "priority"}


def test_gemini_cli_cannot_set_effort():
    assert GeminiCliProvider.supported_efforts is None


# ---------------------------------------------------------------- recording


def test_describe_run_reports_matched_and_unmatched():
    candidates, judge = load_agents(_config(effort="low"))
    s = describe_run(_config(effort="low"), candidates, judge)
    assert s["effort_matched"] is True and s["effort_by_candidate"] == {"a": "low", "b": "low"}

    config = _config()
    config["candidates"][0]["effort"] = "low"
    candidates, judge = load_agents(config)
    s = describe_run(config, candidates, judge)
    assert s["effort_matched"] is False and s["effort_by_candidate"] == {"a": "low", "b": "default"}

    candidates, judge = load_agents(_config())
    assert describe_run(_config(), candidates, judge)["effort_matched"] is False  # "all default" isn't matched


def test_report_header_and_summary_carry_the_settings():
    results = [TaskResult(task=Task(id="t", category="x", prompt="p"),
                          results=[CandidateResult(candidate="a", model="m")])]
    matched = {"effort_matched": True, "effort_by_candidate": {"a": "low"},
               "candidates": {"a": {"provider": "P", "model": "gpt-6-astra", "effort": "low",
                                    "cli_version": "codex-cli 0.153.4", "user_config": {"service_tier": "priority"}}},
               "judge": {"provider": "ClaudeCodeProvider", "model": "claude-opus-5", "effort": "default"}}
    md = to_markdown(results, settings=matched)
    assert "matched — `low` for every candidate" in md
    assert "| a | `gpt-6-astra` | low | codex-cli 0.153.4; personal Codex config: service_tier=priority |" in md
    assert '"run_settings"' in to_summary_json(results, settings=matched)

    unmatched = {**matched, "effort_matched": False, "effort_by_candidate": {"a": "low", "b": "default"}}
    assert "Reasoning effort NOT matched" in to_markdown(results, settings=unmatched)
    assert "run_settings" not in to_summary_json(results)  # optional: old callers unaffected
