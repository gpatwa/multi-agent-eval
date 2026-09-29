"""rejudge.py quota handling: reuse good verdicts on --resume, re-judge the
rest, and stop early instead of burning through a dead judge."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

from eval_agents.judge import Verdict
from eval_agents.runner import CandidateResult, Task, TaskResult

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rejudge", ROOT / "scripts" / "rejudge.py")
rj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rj)


def _results(answers, verdicts=None):
    """One task per answer, one candidate 'c'. verdicts: optional {i: Verdict}."""
    out = []
    for i, a in enumerate(answers):
        r = CandidateResult(candidate="c", model="m", answer=a)
        if verdicts and i in verdicts:
            r.verdict = verdicts[i]
        out.append(TaskResult(task=Task(id=f"t{i}", category="x", prompt="p"), results=[r]))
    return out


OK = lambda score=4.0: Verdict(scores={"q": 4}, overall=score)  # noqa: E731
BAD = lambda: Verdict(parse_error="judge: usage limit")  # noqa: E731


class CountingScorer:
    """Succeeds for answers not in `fail`; records what it was asked to judge."""

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def __call__(self, judge, task, answer):
        self.calls.append(task.id)
        return BAD() if answer in self.fail else OK(5.0)


def test_resume_reuses_good_verdicts_and_judges_only_the_rest():
    source = _results(["a", "b", "c"])
    partial = _results(["a", "b", "c"], {0: OK(3.0), 1: BAD(), 2: OK(3.5)})
    reuse = rj.reusable_verdicts(source, partial)
    assert set(reuse) == {("t0", "c", 0), ("t2", "c", 0)}  # the failed one is not reusable
    scorer = CountingScorer()
    results, stats = rj.rejudge(source, None, scorer, reuse)
    assert scorer.calls == ["t1"]
    assert stats == {"reused": 2, "judged": 1, "failed": 0, "aborted": False}
    assert [tr.results[0].verdict.overall for tr in results] == [3.0, 5.0, 3.5]


def test_verdict_not_reused_when_the_answer_changed():
    source = _results(["a", "CHANGED"])
    partial = _results(["a", "b"], {0: OK(), 1: OK()})
    assert set(rj.reusable_verdicts(source, partial)) == {("t0", "c", 0)}


def test_aborts_after_consecutive_failures_and_marks_the_rest_unjudged():
    source = _results(["a"] * 6)
    scorer = CountingScorer(fail={"a"})
    results, stats = rj.rejudge(source, None, scorer, max_consecutive_failures=3)
    assert len(scorer.calls) == 3  # stopped hammering a dead judge
    assert stats["aborted"] and stats["failed"] == 6
    tail = results[-1].results[0].verdict
    assert tail.parse_error and "not attempted" in tail.parse_error  # counted as unjudged, not missing


def test_success_resets_the_failure_streak():
    source = _results(["bad", "bad", "good", "bad", "bad", "good"])
    scorer = CountingScorer(fail={"bad"})
    _, stats = rj.rejudge(source, None, scorer, max_consecutive_failures=3)
    assert not stats["aborted"] and stats["failed"] == 4 and stats["judged"] == 2


def test_candidate_errors_are_skipped():
    source = _results(["a", "b"])
    source[0].results[0].error = "RuntimeError: boom"
    scorer = CountingScorer()
    rj.rejudge(source, None, scorer)
    assert scorer.calls == ["t1"]


def test_cli_resume_from_a_complete_output_reuses_everything(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=180)

    src, first, second = tmp_path / "src", tmp_path / "first", tmp_path / "second"
    assert run(str(ROOT / "main.py"), "--config", "config.triage.demo.yaml", "--out", str(src)).returncode == 0
    args = [str(ROOT / "scripts" / "rejudge.py"), str(src), "--config", "config.triage.demo.yaml"]
    assert run(*args, "--out", str(first)).returncode == 0
    proc = run(*args, "--resume", str(first), "--out", str(second))
    assert proc.returncode == 0, proc.stderr
    assert "0 newly judged" in proc.stderr and "reusing 120 verdicts" in proc.stderr
    verdicts = lambda d: [r["verdict"]["overall"] for tr in json.loads((d / "results.json").read_text()) for r in tr["results"]]  # noqa: E731
    assert verdicts(first) == verdicts(second)
