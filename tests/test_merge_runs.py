"""scripts/merge_runs.py: combine candidates from separate runs, refusing
merges that wouldn't be a fair comparison."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from eval_agents.judge import Verdict
from eval_agents.report import to_json, to_summary_json
from eval_agents.runner import CandidateResult, Task, TaskResult

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("merge_runs", ROOT / "scripts" / "merge_runs.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)

JUDGE = {"provider": "ClaudeCodeProvider", "model": "claude-opus-5", "effort": "default"}


def _run(tmp_path, name, candidate, latency, tasks=("t1", "t2"), effort="low", judge=JUDGE):
    results = [TaskResult(task=Task(id=t, category="x", prompt="p"),
                          results=[CandidateResult(candidate=candidate, model="m", latency_s=latency,
                                                   verdict=Verdict(scores={"q": 5}, overall=5.0))])
               for t in tasks]
    settings = {"effort_matched": True, "effort_by_candidate": {candidate: effort},
                "candidates": {candidate: {"provider": "P", "model": f"{candidate}-model", "effort": effort}}, "judge": judge}
    d = tmp_path / name
    d.mkdir()
    (d / "results.json").write_text(to_json(results))
    (d / "summary.json").write_text(to_summary_json(results, settings=settings))
    return d


def test_merges_candidates_and_renormalizes_across_the_union(tmp_path):
    a, b = _run(tmp_path, "a", "fast", 1.0), _run(tmp_path, "b", "slow", 9.0)
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "merge_runs.py"), str(a), str(b),
                           "--config", "config.triage.mixed.yaml", "--out", str(out)],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads((out / "summary.json").read_text())
    assert set(summary["candidates"]) == {"fast", "slow"}
    # latency is min-max normalised over BOTH candidates, so the fast one wins the composite
    assert summary["ranking"] == ["fast", "slow"]
    assert summary["run_settings"]["effort_matched"] is True
    assert summary["run_settings"]["merged_from"] == [str(a), str(b)]
    assert "matched — `low` for every candidate" in (out / "report.md").read_text()


def test_can_take_only_named_candidates_from_a_run(tmp_path):
    both = _run(tmp_path, "both", "good", 1.0)
    # a run whose second candidate is unusable (e.g. hit a usage limit): take only "good" from it
    a = mr.load_results(both, with_verdicts=True)
    picked, settings = mr.pick_candidates(a, mr.load_settings(both), ["good"], "both")
    assert [r.candidate for tr in picked for r in tr.results] == ["good", "good"] and list(settings["candidates"]) == ["good"]
    with pytest.raises(mr.MergeError, match="has no candidate"):
        mr.pick_candidates(a, mr.load_settings(both), ["nope"], "both")
    assert mr.parse_spec("runs/a:x,y") == (pathlib.Path("runs/a"), ["x", "y"]) and mr.parse_spec("runs/a") == (pathlib.Path("runs/a"), None)


def test_refuses_runs_over_different_tasks(tmp_path):
    a, b = _run(tmp_path, "a", "x", 1.0), _run(tmp_path, "b", "y", 1.0, tasks=("t1", "t3"))
    with pytest.raises(mr.MergeError, match="different tasks"):
        mr.merge_results([mr.load_results(a, with_verdicts=True), mr.load_results(b, with_verdicts=True)], ["a", "b"])


def test_refuses_duplicate_candidate_names(tmp_path):
    a, b = _run(tmp_path, "a", "same", 1.0), _run(tmp_path, "b", "same", 2.0)
    with pytest.raises(mr.MergeError, match="appears in both"):
        mr.merge_results([mr.load_results(a, with_verdicts=True), mr.load_results(b, with_verdicts=True)], ["a", "b"])


def test_refuses_different_judges_unless_overridden(tmp_path):
    other = {**JUDGE, "model": "gpt-6-astra", "provider": "CodexProvider"}
    sa, sb = mr.load_settings(_run(tmp_path, "a", "x", 1.0)), mr.load_settings(_run(tmp_path, "b", "y", 1.0, judge=other))
    with pytest.raises(mr.MergeError, match="judged differently"):
        mr.merge_settings([sa, sb], ["a", "b"], allow_judge_mismatch=False)
    merged = mr.merge_settings([sa, sb], ["a", "b"], allow_judge_mismatch=True)
    assert "judges differ" in merged["notes"][0]


def test_judge_cli_version_difference_is_noted_not_refused(tmp_path):
    v1, v2 = {**JUDGE, "cli_version": "2.1.263"}, {**JUDGE, "cli_version": "2.1.270"}
    sa, sb = mr.load_settings(_run(tmp_path, "a", "x", 1.0, judge=v1)), mr.load_settings(_run(tmp_path, "b", "y", 1.0, judge=v2))
    merged = mr.merge_settings([sa, sb], ["a", "b"], allow_judge_mismatch=False)
    assert "CLI version differs" in merged["notes"][0]


def test_unmatched_effort_across_runs_is_reported(tmp_path):
    sa, sb = mr.load_settings(_run(tmp_path, "a", "x", 1.0, effort="low")), mr.load_settings(_run(tmp_path, "b", "y", 1.0, effort="high"))
    merged = mr.merge_settings([sa, sb], ["a", "b"], False)
    assert merged["effort_matched"] is False and merged["effort_by_candidate"] == {"x": "low", "y": "high"}


def test_runs_without_recorded_settings_cannot_be_merged(tmp_path):
    with pytest.raises(mr.MergeError, match="no run_settings"):
        mr.merge_settings([None, mr.load_settings(_run(tmp_path, "b", "y", 1.0))], ["old", "b"], False)
