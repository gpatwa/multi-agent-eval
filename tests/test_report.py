"""Tests for the balanced-scorecard math in report.summarize(): composite
ranking, guardrail-violation counting, and latency percentiles. These are
the numbers a real deployment decision gets made from, so the arithmetic
needs a harness that doesn't depend on a live API call to exercise."""
from __future__ import annotations

from eval_agents.judge import Verdict
from eval_agents.report import summarize
from eval_agents.runner import CandidateResult, Task, TaskResult


def _task(i: int) -> Task:
    return Task(id=f"t{i}", category="ticket", prompt="...", gold={})


def _result(candidate: str, quality: float, latency: float, flags=None) -> CandidateResult:
    return CandidateResult(
        candidate=candidate,
        model=f"{candidate}-model",
        latency_s=latency,
        input_tokens=100,
        output_tokens=50,
        verdict=Verdict(scores={"quality": round(quality)}, overall=quality, flags=flags or []),
    )


def test_quality_only_ranking_no_scorecard():
    results = [
        TaskResult(task=_task(0), results=[_result("fast", 3.0, 1.0), _result("good", 5.0, 10.0)]),
    ]
    summary = summarize(results, scorecard=None)
    assert summary["ranking"] == ["good", "fast"]
    assert summary["candidates"]["good"]["quality_mean"] == 5.0


def test_balanced_scorecard_weighs_latency():
    """Two candidates score identically on quality; the faster one should
    win once latency is weighted into the composite."""
    results = [
        TaskResult(task=_task(0), results=[_result("slow", 4.0, 100.0), _result("fast", 4.0, 1.0)]),
    ]
    scorecard = {"weights": {"quality": 0.5, "latency": 0.5, "cost": 0.0}}
    summary = summarize(results, scorecard)
    assert summary["ranking"][0] == "fast"
    assert summary["candidates"]["fast"]["composite"] > summary["candidates"]["slow"]["composite"]


def test_critical_violation_counted_and_not_folded_into_quality():
    results = [
        TaskResult(
            task=_task(0),
            results=[_result("risky", 5.0, 1.0, flags=["policy_critical"])],
        ),
    ]
    summary = summarize(results, scorecard=None)
    c = summary["candidates"]["risky"]
    assert c["critical_violations"] == 1
    assert c["flag_counts"] == {"policy_critical": 1}
    # The 5.0 quality score still counts toward the mean — violations are a
    # separate gate, not a score penalty (that's a report-rendering choice,
    # not a math bug).
    assert c["quality_mean"] == 5.0


def test_latency_percentiles():
    latencies = [1.0, 2.0, 3.0, 4.0, 100.0]  # one outlier
    results = [
        TaskResult(task=_task(i), results=[_result("c", 5.0, lat)])
        for i, lat in enumerate(latencies)
    ]
    summary = summarize(results, scorecard=None)
    c = summary["candidates"]["c"]
    assert c["latency_p50"] == 3.0
    assert c["latency_p95"] == 100.0  # p95 must surface the tail, not average it away


def test_errors_excluded_from_quality_but_counted():
    results = [
        TaskResult(
            task=_task(0),
            results=[
                CandidateResult(candidate="flaky", model="m", error="RuntimeError: boom"),
                _result("flaky", 5.0, 1.0),
            ],
        ),
    ]
    summary = summarize(results, scorecard=None)
    c = summary["candidates"]["flaky"]
    assert c["errors"] == 1
    assert c["quality_mean"] == 5.0  # error sample doesn't drag down quality


def test_cost_projection_uses_pricing_map():
    results = [TaskResult(task=_task(0), results=[_result("priced", 5.0, 1.0)])]
    scorecard = {"pricing": {"priced": [10.0, 20.0]}}  # $/MTok in, out
    summary = summarize(results, scorecard)
    c = summary["candidates"]["priced"]
    # 100 input tok * $10/1e6 + 50 output tok * $20/1e6
    assert c["cost_per_task"] == round(100 * 10.0 / 1e6 + 50 * 20.0 / 1e6, 6)
    assert c["priced"] is True


def test_unpriced_candidate_is_flat_rate():
    results = [TaskResult(task=_task(0), results=[_result("subscription", 5.0, 1.0)])]
    summary = summarize(results, scorecard={"pricing": {}})
    assert summary["candidates"]["subscription"]["priced"] is False
    assert summary["candidates"]["subscription"]["cost_per_task"] == 0.0
