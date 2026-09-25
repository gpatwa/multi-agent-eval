"""AutomationBench lane: task loading, the sandbox bridge onto their
WorldState/tools, the executor, and the scorer. Skipped when the optional
AutomationBench package isn't installed (Python >= 3.13 extra)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("automationbench")

from eval_agents.agents import Agent  # noqa: E402
from eval_agents.config import load_tasks  # noqa: E402
from eval_agents.providers.base import Provider, ToolCall, ToolTurn  # noqa: E402
from eval_agents.usecases import automationbench as ab  # noqa: E402


def test_load_full_domain_and_stable_sample():
    everything = load_tasks("automationbench:support")
    assert len(everything) == 100
    assert len({t.id for t in everything}) == 100
    sample = [t.id for t in load_tasks("automationbench:support:7")]
    assert len(sample) == 7
    assert sample == [t.id for t in load_tasks("automationbench:support:7")]  # same tasks every run
    # the web UI joins tasks_file onto the repo root
    assert [t.id for t in load_tasks("/repo/automationbench:support:7")] == sample
    t = everything[0]
    assert t.category == "automationbench/support"
    assert set(t.fixture["automationbench"]) == {"domain", "example_id"}  # pointer, not the world


def test_unwired_domain_is_a_clear_error():
    with pytest.raises(RuntimeError, match="not wired"):
        load_tasks("automationbench:sales:3")


def _row():
    task = load_tasks("automationbench:support:1")[0]
    ref = task.fixture["automationbench"]
    return task, ab._dataset(ref["domain"])[ref["example_id"]]


def test_sandbox_exposes_only_the_tasks_tools_and_reports_errors():
    _, row = _row()
    box = ab.ABSandbox(row["info"])
    assert [t.name for t in box.tools] == row["info"]["zapier_tools"]
    not_allowed = next(n for n in ab._tooling()[0] if n not in row["info"]["zapier_tools"])
    assert box.execute(ToolCall(id="1", name=not_allowed, arguments={})).is_error
    allowed = row["info"]["zapier_tools"][0]
    bad = box.execute(ToolCall(id="2", name=allowed, arguments={"no_such_argument": 1}))
    assert bad.is_error and box.errors == 2


def test_doing_nothing_does_not_pass():
    _, row = _row()
    score = ab.ABSandbox(row["info"]).score()
    assert score["completed"] is False
    assert score["assertions_scored"] > 0
    assert 0.0 <= score["partial_credit"] < 1.0


class OneReadThenStop(Provider):
    """Calls the task's first tool once with no args, then stops."""
    supports_tools = True

    def complete(self, messages, system=None, max_tokens=4096):
        raise AssertionError

    def complete_with_tools(self, history, tools, system=None, max_tokens=4096):
        self.system_seen = system
        done = any(isinstance(h, list) for h in history)
        calls = [] if done else [ToolCall(id="c1", name=tools[0].name, arguments={})]
        return ToolTurn(text="done" if done else "", model="scripted", tool_calls=calls,
                        input_tokens=100, output_tokens=5)


def test_executor_runs_loop_with_their_system_prompt_and_scores():
    task, row = _row()
    provider = OneReadThenStop("scripted")
    resp = ab.run_candidate(Agent(name="c", provider=provider, system="ignored"), task)
    record = json.loads(resp.text)
    assert provider.system_seen == row["system"]  # AutomationBench's prompt, not ours
    assert record["tool_calls"] == 1 and record["cut_off"] is False
    assert (resp.input_tokens, resp.output_tokens) == (200, 10)
    v = ab.automationbench_scorer(None, task, resp.text)
    assert v.passed is False
    assert v.overall == round(1 + 4 * record["partial_credit"], 2)
    assert f"/{record['assertions_scored']} assertions" in v.rationale


def test_scorer_full_credit_passes():
    record = {"partial_credit": 1.0, "completed": True, "assertions_passed": 9, "assertions_scored": 9,
              "failed": [], "tool_calls": 4, "tool_errors": 0, "cut_off": False}
    v = ab.automationbench_scorer(None, None, json.dumps(record))
    assert v.passed is True and v.overall == 5.0 and v.scores == {"assertions": 5.0}


def test_scorer_unreadable_answer():
    v = ab.automationbench_scorer(None, None, "nope")
    assert v.passed is False and v.overall == 1.0


def test_runs_off_the_main_thread_and_leaves_sigint_alone():
    """Candidates execute in worker threads; AutomationBench's env object
    would install a SIGINT handler (main-thread only) and hijack Ctrl-C."""
    import signal
    import threading

    ab._tooling.cache_clear()
    before = signal.getsignal(signal.SIGINT)
    task, row = _row()
    errors = []

    def work():
        try:
            box = ab.ABSandbox(row["info"])
            box.score()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    th = threading.Thread(target=work)
    th.start()
    th.join()
    assert not errors
    assert signal.getsignal(signal.SIGINT) is before


def test_tool_schemas_match_automationbench_env():
    """Guards the hand-mirrored add_tool() logic across pin bumps."""
    import signal

    from automationbench.rubric import create_rubric
    from automationbench.runner import AutomationBenchEnv
    from datasets import Dataset

    before = signal.getsignal(signal.SIGINT)
    try:
        env = AutomationBenchEnv(dataset=Dataset.from_list([{"prompt": [], "answer": "", "info": "{}"}]),
                                 rubric=create_rubric(), toolset="limited_zapier")
        theirs = {t.name: t for t in env._all_tool_defs}
    finally:
        signal.signal(signal.SIGINT, before)  # the env installs its own handler
    ours = ab._tooling()[0]
    assert set(ours) == set(theirs)
    for name, t in theirs.items():
        assert ours[name].parameters == t.parameters, name
        assert ours[name].description == t.description, name
