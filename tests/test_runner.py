"""Tests for eval_agents.runner.run_evaluation's own orchestration logic:
multi-trial fan-out and, most importantly, that a genuinely unexpected
failure mid-run (a scorer bug, Ctrl-C) still surfaces everything completed
so far via `.partial_results` on the raised exception, rather than losing
it. This is what main.py and webapp/server.py both rely on to avoid
discarding a long, expensive real-provider run on one late failure."""
from __future__ import annotations

import pytest

from eval_agents.judge import Verdict
from eval_agents.runner import Task, run_evaluation


def _agent(name: str, text: str = "answer"):
    from eval_agents.agents import Agent
    from tests.conftest import FakeProvider

    return Agent(name=name, provider=FakeProvider(model=f"{name}-model", response_text=text))


def _ok_scorer(judge, task, answer):
    return Verdict(scores={"quality": 5}, overall=5.0)


def test_all_tasks_complete_normally():
    tasks = [Task(id=f"t{i}", category="c", prompt="p") for i in range(3)]
    candidates = [_agent("a"), _agent("b")]
    results = run_evaluation(tasks, candidates, _agent("judge"), scorer=_ok_scorer)
    assert len(results) == 3
    assert all(len(tr.results) == 2 for tr in results)


def test_trials_multiply_results_per_task():
    tasks = [Task(id="t0", category="c", prompt="p")]
    candidates = [_agent("a")]
    results = run_evaluation(tasks, candidates, _agent("judge"), scorer=_ok_scorer, trials=3)
    assert len(results) == 1
    assert len(results[0].results) == 3
    assert {r.trial for r in results[0].results} == {0, 1, 2}


def test_scorer_bug_attaches_partial_results_to_exception():
    """A scorer that raises on the second task is a genuinely unexpected
    failure (not a judge-transport error, which is already caught inside
    the scorer itself) -- the first task's results must not be lost."""
    tasks = [Task(id=f"t{i}", category="c", prompt="p") for i in range(3)]
    candidates = [_agent("a")]

    calls = {"n": 0}

    def flaky_scorer(judge, task, answer):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("unexpected bug in scorer")
        return Verdict(scores={"quality": 5}, overall=5.0)

    with pytest.raises(RuntimeError) as exc_info:
        run_evaluation(tasks, candidates, _agent("judge"), scorer=flaky_scorer)

    partial = exc_info.value.partial_results
    assert len(partial) == 1  # only the first task completed before the raise
    assert partial[0].task.id == "t0"


def test_no_completed_tasks_yields_empty_partial_results():
    def always_raises(judge, task, answer):
        raise RuntimeError("boom before anything scores")

    with pytest.raises(RuntimeError) as exc_info:
        run_evaluation(
            [Task(id="t0", category="c", prompt="p")], [_agent("a")], _agent("judge"),
            scorer=always_raises,
        )
    assert exc_info.value.partial_results == []
