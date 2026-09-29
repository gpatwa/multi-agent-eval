"""The publish gate: data may only be published if it supports the claims the page makes."""
from __future__ import annotations

import json

import pytest

from eval_agents.judge import Verdict
from eval_agents.publish import (
    GateError, agreement_view, assert_publishable, build_release, check_gates, judge_sentence,
    leak_probes, pending_data, suite_view, walkthrough_view,
)
from eval_agents.report import to_json, to_summary_json
from eval_agents.runner import CandidateResult, Task, TaskResult

JUDGE_A = {"provider": "ClaudeCodeProvider", "model": "claude-opus-5", "effort": "default"}
JUDGE_B = {"provider": "CodexProvider", "model": "gpt-6-astra", "effort": "low"}


def _verdict(scores, flags=(), rationale="routed billing/normal vs gold billing/normal. All good.", overall=None):
    return Verdict(scores=scores, overall=overall or round(sum(scores.values()) / len(scores), 2), rationale=rationale, flags=list(flags))


def make_run(tmp_path, name, candidates, *, judge=JUDGE_A, matched=True, tasks=None, verdicts=None, errors=(), latency=None):
    """A real run directory (results.json + summary.json with settings) for `candidates`."""
    tasks = tasks or [("t1", "ticket"), ("t2", "ticket"), ("t3", "guardrail")]
    latency = latency or {c: 5.0 + i * 5 for i, c in enumerate(candidates)}
    results = []
    for tid, cat in tasks:
        rs = []
        for c in candidates:
            v = (verdicts or {}).get((tid, c)) or _verdict({"routing": 5, "priority": 5, "policy_adherence": 5, "resolution": 4, "tone": 5})
            answer = json.dumps({"category": "billing", "priority": "normal", "reply": f"Reply from {c} for {tid}. " * 3})
            rs.append(CandidateResult(candidate=c, model=f"{c}-m", latency_s=latency[c], answer=answer,
                                      error="boom" if (tid, c) in errors else None, verdict=None if (tid, c) in errors else v))
        results.append(TaskResult(task=Task(id=tid, category=cat, prompt=f"Subject: {tid} subject\nBody text for {tid} that is long enough to matter.",
                                            gold={"category": "billing", "priority": "normal"}), results=rs))
    efforts = {c: "low" if matched else ("low" if i == 0 else "high") for i, c in enumerate(candidates)}
    settings = {"effort_matched": matched, "effort_by_candidate": efforts,
                "candidates": {c: {"provider": "P", "model": f"{c}-model", "effort": efforts[c]} for c in candidates}, "judge": judge}
    d = tmp_path / name
    d.mkdir()
    (d / "results.json").write_text(to_json(results))
    (d / "summary.json").write_text(to_summary_json(results, {"weights": {"quality": 0.7, "latency": 0.3}}, settings=settings))
    return d


CANDS = ["claude", "gpt", "gemini"]


def _release(tmp_path, *, second=True, **kw):
    public = make_run(tmp_path, "pub", CANDS, **kw)
    held = make_run(tmp_path, "held", CANDS, tasks=[("h1", "ticket"), ("h2", "guardrail")], **kw)
    sec = {"public": make_run(tmp_path, "pub2", CANDS, judge=JUDGE_B), "heldout": make_run(tmp_path, "held2", CANDS, tasks=[("h1", "ticket"), ("h2", "guardrail")], judge=JUDGE_B)} if second else None
    return build_release(suites={"public": public, "heldout": held}, second=sec, walkthrough_task="t3", providers=19)


GATE = dict(required_candidates=CANDS, require_second_judge=True)


def test_valid_release_passes_every_gate(tmp_path):
    data = _release(tmp_path)
    assert check_gates(data, **GATE) == []
    assert data["suites"]["public"]["n_tasks"] == 3 and data["suites"]["public"]["n_guardrail"] == 1
    assert data["suites"]["heldout"]["n_tasks"] == 2
    assert data["agreement"]["public"]["coverage"] == 1.0


def test_unmatched_effort_blocks_publishing(tmp_path):
    problems = check_gates(_release(tmp_path, matched=False), **GATE)
    assert any("effort not matched" in p for p in problems)


def test_missing_candidate_and_candidate_errors_block(tmp_path):
    data = _release(tmp_path)
    assert any("missing candidate" in p for p in check_gates(data, required_candidates=CANDS + ["grok"], require_second_judge=True))
    public = make_run(tmp_path, "pub_err", CANDS, errors={("t1", "gpt")})
    data = build_release(suites={"public": public}, second=None, walkthrough_task="t3", providers=19)
    assert any("gpt: 1 candidate error" in p for p in check_gates(data, required_candidates=CANDS, require_second_judge=False))


def test_low_judge_coverage_blocks(tmp_path):
    bad = {("t1", "claude"): Verdict(parse_error="judge: usage limit"), ("t2", "claude"): Verdict(parse_error="judge: usage limit")}
    public = make_run(tmp_path, "pub_cov", CANDS, verdicts=bad)
    data = build_release(suites={"public": public}, second=None, walkthrough_task="t3", providers=19)
    problems = check_gates(data, required_candidates=CANDS, require_second_judge=False)
    assert any("claude" in p and "unjudged" in p for p in problems)


def test_second_judge_required_and_must_be_a_different_vendor(tmp_path):
    assert any("second-judge agreement required" in p for p in check_gates(_release(tmp_path, second=False), **GATE))
    same = build_release(suites={"public": make_run(tmp_path, "p", CANDS)}, second={"public": make_run(tmp_path, "p2", CANDS, judge=JUDGE_A)},
                         walkthrough_task="t3", providers=19)
    assert any("same vendor" in p for p in check_gates(same, required_candidates=CANDS, require_second_judge=True))
    assert check_gates(same, required_candidates=CANDS, require_second_judge=False) != []  # still a problem when present


def test_partially_judged_second_judge_blocks(tmp_path):
    bad = {(t, c): Verdict(parse_error="judge: usage limit") for t in ("t1", "t2") for c in CANDS}
    sec = make_run(tmp_path, "sec", CANDS, judge=JUDGE_B, verdicts=bad)
    data = build_release(suites={"public": make_run(tmp_path, "p", CANDS)}, second={"public": sec}, walkthrough_task="t3", providers=19)
    assert data["agreement"]["public"]["coverage"] < 0.5
    assert any("second judge covered only" in p for p in check_gates(data, **GATE))


def test_different_judges_across_suites_block(tmp_path):
    public = make_run(tmp_path, "p", CANDS)
    held = make_run(tmp_path, "h", CANDS, judge=JUDGE_B)
    data = build_release(suites={"public": public, "heldout": held}, second=None, walkthrough_task="t3", providers=19)
    assert any("different judges" in p for p in check_gates(data, required_candidates=CANDS, require_second_judge=False))


def test_held_out_text_never_leaks_into_public_data(tmp_path):
    data = _release(tmp_path, second=False)
    priv = [Task(id="h1", category="ticket", prompt="Subject: x\nBody text for h1 that is long enough to matter.")]
    # the held-out suite's aggregates are safe...
    assert not any("held-out" in p for p in check_gates(data, required_candidates=CANDS, require_second_judge=False, private_probes=leak_probes(priv)))
    # ...but a walkthrough that quotes a held-out ticket is caught
    data["walkthrough"] = walkthrough_view(tmp_path / "held", "h1")
    assert any("held-out ticket text" in p for p in check_gates(data, required_candidates=CANDS, require_second_judge=False, private_probes=leak_probes(priv)))


def test_pending_data_is_never_publishable():
    data = pending_data(suites={"public": {"n_tasks": 40, "n_guardrail": 7}}, providers=19, reason="waiting for gpt")
    assert check_gates(data, **GATE) == ["status is 'pending'"]
    with pytest.raises(GateError):
        assert_publishable(data, **GATE)


def test_agreement_view_measures_per_answer_agreement(tmp_path):
    lo = _verdict({"routing": 5, "priority": 5, "policy_adherence": 3, "resolution": 3, "tone": 3})
    a = make_run(tmp_path, "a", ["x", "y"], verdicts={("t1", "x"): lo})
    b = make_run(tmp_path, "b", ["x", "y"], judge=JUDGE_B)  # second judge scores t1/x as 5,4,5 instead of 3,3,3
    ag = agreement_view(a, b)
    assert ag["coverage"] == 1.0 and ag["n_answers"] == 6
    assert ag["dimensions"]["policy_adherence"]["within1"] < 1.0 and set(ag["dimensions"]) == {"policy_adherence", "resolution", "tone"}
    assert ag["rank_rho"] == 1.0


def test_judge_sentence_strips_the_routing_note_and_flags():
    assert judge_sentence("routed billing/normal vs gold billing/normal. The reply is fine.") == "The reply is fine."
    assert judge_sentence("routed cancellation/normal vs gold account_access/normal; action miss: refund,escalate. FLAGS: policy_critical. Bad reply.") == "Bad reply."
    assert judge_sentence("plain rationale") == "plain rationale"


def test_walkthrough_quotes_the_real_ticket_and_replies(tmp_path):
    w = walkthrough_view(make_run(tmp_path, "w", CANDS), "t3")
    assert w["subject"] == "t3 subject" and w["body"].startswith("Body text for t3")
    assert [c["candidate"] for c in w["cards"]] == CANDS and w["cards"][0]["reply_excerpt"].startswith("Reply from claude for t3.")
    assert walkthrough_view(tmp_path / "w", "nope") is None


def test_suite_view_reads_settings_and_no_ticket_text(tmp_path):
    view = suite_view(make_run(tmp_path, "s", CANDS))
    assert view["effort_matched"] and view["candidates"]["gpt"]["effort"] == "low" and view["judge"] == JUDGE_A
    assert "Body text" not in json.dumps(view)
