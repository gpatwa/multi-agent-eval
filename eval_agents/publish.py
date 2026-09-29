"""From finished runs to publishable data — and the gate that decides if it may be published.

The landing page states facts about eval results (who ranks first, how many
violations, whether two judges agree). Those statements must come from data,
and only from data that is *valid*. So publishing has two parts:

  1. build_release() reduces finished run directories to one small, sanitized
     data document (docs/data/results.json) — the only input the page renders.
  2. check_gates() refuses that document unless the runs support the claims the
     page makes: matched reasoning effort, complete judge coverage, one judge
     across suites, every required candidate present and error-free, and no
     held-out ticket text anywhere in the public data.

Everything here is a pure function of files on disk (no network, no models), so
it is cheap to test and safe to run unattended.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import statistics
from datetime import datetime, timezone

from .json_extract import extract_json
from .results_io import load_results, load_settings

SCHEMA = 1
DETERMINISTIC_DIMS = ("routing", "priority", "actions")  # graded without a judge
MIN_JUDGE_COVERAGE = 0.90
_ROUTE_NOTE = re.compile(r"^routed \S+ vs gold \S+?(?:; action miss: [\w,]+)?\.(?: FLAGS: [\w,]+\.)? ?(.*)$", re.S)


class GateError(RuntimeError):
    """Raised when data can't be published; carries every reason found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------- views


def _summary(run_dir: pathlib.Path) -> dict:
    return json.loads((pathlib.Path(run_dir) / "summary.json").read_text())


def topic_view(raw: list[dict]) -> dict:
    """Quality and latency per topic per candidate. A topic is the ticket's gold
    routing label, or "guardrail" for the adversarial tasks: labels only, never
    ticket text, so it is as safe to publish for a held-out suite as the totals."""
    cells: dict[str, dict[str, dict]] = {}
    for tr in raw:
        task = tr["task"]
        topic = "guardrail" if task["category"] == "guardrail" else str((task.get("gold") or {}).get("category") or task["category"])
        for r in tr["results"]:
            if r.get("error"):
                continue
            cell = cells.setdefault(topic, {}).setdefault(r["candidate"], {"n": 0, "quality": [], "latency": []})
            cell["n"] += 1
            cell["latency"].append(r["latency_s"])
            v = r.get("verdict")
            if v and not v.get("parse_error"):
                cell["quality"].append(v["overall"])
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    return {t: {"n_tasks": len({tr["task"]["id"] for tr in raw if (
                    "guardrail" if tr["task"]["category"] == "guardrail"
                    else str((tr["task"].get("gold") or {}).get("category") or tr["task"]["category"])) == t}),
                "candidates": {c: {"n": v["n"], "quality_mean": mean(v["quality"]), "latency_mean": mean(v["latency"])}
                               for c, v in sorted(by.items())}}
            for t, by in sorted(cells.items())}


def suite_view(run_dir: pathlib.Path) -> dict:
    """Aggregates for one suite. Reads task *categories* only, never ticket text,
    so it is safe for a held-out suite."""
    run_dir = pathlib.Path(run_dir)
    summary = _summary(run_dir)
    settings = summary.get("run_settings") or {}
    raw = json.loads((run_dir / "results.json").read_text())
    per_candidate = settings.get("candidates", {})
    candidates = {}
    for name, s in summary["candidates"].items():
        info = per_candidate.get(name, {})
        candidates[name] = {
            "model": info.get("model", s.get("model")),
            "provider": info.get("provider"),
            "effort": info.get("effort", "default"),
            "cli_version": info.get("cli_version"),
            "service_tier": (info.get("user_config") or {}).get("service_tier"),
            "composite": s["composite"],
            "quality_mean": s["quality_mean"],
            "quality_ci95": s["quality_ci95"],
            "latency_p50": s["latency_p50"],
            "latency_p95": s["latency_p95"],
            "critical_violations": s["critical_violations"],
            "flag_counts": s["flag_counts"],
            "errors": s["errors"],
            "n_samples": s["n_samples"],
            "judge_failure_rate": s.get("judge_failure_rate", 0.0),
        }
    return {
        "n_tasks": len(raw),
        "n_guardrail": sum(1 for tr in raw if tr["task"]["category"] == "guardrail"),
        "ranking": summary["ranking"],
        "weights": summary["weights"],
        "effort_matched": bool(settings.get("effort_matched")),
        "judge": settings.get("judge"),
        "candidates": candidates,
        "topics": topic_view(raw),
    }


def _pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    mx, my = statistics.mean(x), statistics.mean(y)
    den = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return None if den == 0 else round(sum((a - mx) * (b - my) for a, b in zip(x, y)) / den, 3)


def _spearman(order_a: list[str], order_b: list[str]) -> float | None:
    common = [c for c in order_a if c in order_b]
    n = len(common)
    if n < 2:
        return None
    ra = {c: i for i, c in enumerate(c for c in order_a if c in common)}
    rb = {c: i for i, c in enumerate(c for c in order_b if c in common)}
    return round(1 - 6 * sum((ra[c] - rb[c]) ** 2 for c in common) / (n * (n**2 - 1)), 3)


def agreement_view(primary_dir: pathlib.Path, second_dir: pathlib.Path) -> dict:
    """How two judges scored the SAME answers (the second run is a rejudge of the first)."""
    a, b = load_results(primary_dir, with_verdicts=True), load_results(second_dir, with_verdicts=True)
    pairs, total = [], 0
    second = {(tr.task.id, r.candidate, r.trial): r for tr in b for r in tr.results}
    for tr in a:
        for r in tr.results:
            if r.error:
                continue
            total += 1
            other = second.get((tr.task.id, r.candidate, r.trial))
            va, vb = r.verdict, other.verdict if other else None
            if va and vb and not va.parse_error and not vb.parse_error:
                pairs.append((va, vb))
    dims = [d for d in (pairs[0][0].scores if pairs else {}) if d not in DETERMINISTIC_DIMS and d in pairs[0][1].scores]
    per_dim = {}
    for d in dims:
        x, y = [p[0].scores[d] for p in pairs], [p[1].scores[d] for p in pairs]
        diff = [abs(p - q) for p, q in zip(x, y)]
        per_dim[d] = {
            "exact": round(sum(v == 0 for v in diff) / len(diff), 3),
            "within1": round(sum(v <= 1 for v in diff) / len(diff), 3),
            "pearson": _pearson(x, y),
        }
    sa, sb = _summary(primary_dir), _summary(second_dir)
    flagged = lambda v: bool(set(v.flags) - {"pii_echo"})  # noqa: E731
    return {
        "coverage": round(len(pairs) / total, 3) if total else 0.0,
        "n_answers": len(pairs),
        "dimensions": per_dim,
        "rank_rho": _spearman(sa["ranking"], sb["ranking"]),
        "ranking_second_judge": sb["ranking"],
        "composite_second_judge": {n: c["composite"] for n, c in sb["candidates"].items()},
        "flagged_answers": {"primary": sum(flagged(p[0]) for p in pairs), "second": sum(flagged(p[1]) for p in pairs),
                            "both": sum(flagged(p[0]) and flagged(p[1]) for p in pairs)},
        "judge_primary": (load_settings(primary_dir) or {}).get("judge"),
        "judge_second": (load_settings(second_dir) or {}).get("judge"),
    }


def judge_sentence(rationale: str) -> str:
    """The judge's own sentence from a triage verdict rationale (which is prefixed with
    our routing note and flags)."""
    m = _ROUTE_NOTE.match(rationale or "")
    return (m.group(1) if m else rationale or "").strip()


def _excerpt(text: str, limit: int = 260) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def walkthrough_view(run_dir: pathlib.Path, task_id: str) -> dict | None:
    """One real ticket, verbatim, with every candidate's scores. Public suites only."""
    for tr in load_results(run_dir, with_verdicts=True):
        if tr.task.id != task_id:
            continue
        lines = tr.task.prompt.strip().splitlines()
        subject = lines[0].removeprefix("Subject:").strip() if lines and lines[0].startswith("Subject:") else ""
        body = " ".join(" ".join(lines[1:] if subject else lines).split())
        cards = []
        for r in tr.results:
            if r.error or not r.verdict or r.verdict.parse_error:
                continue
            try:
                answer = extract_json(r.answer)
            except Exception:
                answer = {}
            cards.append({
                "candidate": r.candidate,
                "overall": r.verdict.overall,
                "scores": r.verdict.scores,
                "category": str(answer.get("category", "")),
                "priority": str(answer.get("priority", "")),
                "reply_excerpt": _excerpt(str(answer.get("reply", ""))),
                "judge_says": judge_sentence(r.verdict.rationale),
                "flags": r.verdict.flags,
            })
        return {"task_id": task_id, "subject": subject, "body": body, "gold": tr.task.gold, "cards": cards}
    return None


# ---------------------------------------------------------------- release


def build_release(*, suites: dict[str, pathlib.Path], second: dict[str, pathlib.Path] | None,
                  walkthrough_task: str, providers: int, suite_order: list[str] | None = None, site_url: str = "") -> dict:
    """The published document. `suites[name]` is a merged run directory; `second[name]`
    an optional rejudge of it by a second judge; the first suite is the public one
    (the walkthrough quotes it). `site_url` is where the page is served (release.yaml `site.url`)."""
    order = suite_order or list(suites)
    data = {
        "schema": SCHEMA, "status": "published", "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "providers": providers, "site_url": site_url,
        "suites": {name: suite_view(suites[name]) for name in order},
        "agreement": {name: agreement_view(suites[name], second[name]) for name in order if second and name in second} or None,
        "walkthrough": walkthrough_view(suites[order[0]], walkthrough_task),
    }
    return data


def pending_data(*, suites: dict[str, dict], providers: int, reason: str, site_url: str = "") -> dict:
    """Placeholder the page renders while no valid results exist. `suites[name]` is
    {"n_tasks": .., "n_guardrail": ..} (sizes only)."""
    return {"schema": SCHEMA, "status": "pending", "reason": reason, "providers": providers, "site_url": site_url, "suites": suites}


# ---------------------------------------------------------------- gates


def _coverage_problems(data: dict, min_coverage: float) -> list[str]:
    out = []
    for sname, suite in data["suites"].items():
        for cname, c in suite["candidates"].items():
            if c["errors"]:
                out.append(f"{sname}/{cname}: {c['errors']} candidate error(s)")
            if c["judge_failure_rate"] > 1 - min_coverage:
                out.append(f"{sname}/{cname}: {c['judge_failure_rate']:.0%} of verdicts unjudged (limit {1 - min_coverage:.0%})")
    return out


def leak_probes(private_tasks) -> list[str]:
    """Strings that must never appear in public data: each held-out task id and each
    substantial line of its prompt."""
    probes = []
    for t in private_tasks:
        probes.append(t.id)
        probes += [ln.strip() for ln in t.prompt.splitlines() if len(ln.strip()) >= 20]
    return probes


def check_gates(data: dict, *, required_candidates: list[str], require_second_judge: bool,
                private_probes: list[str] = (), min_coverage: float = MIN_JUDGE_COVERAGE) -> list[str]:
    """Every reason `data` must not be published (empty list = publishable)."""
    if data.get("status") != "published":
        return [f"status is {data.get('status')!r}"]
    problems = []
    judges = set()
    for sname, suite in data["suites"].items():
        missing = [c for c in required_candidates if c not in suite["candidates"]]
        if missing:
            problems.append(f"{sname}: missing candidate(s) {missing}")
        if not suite["effort_matched"]:
            efforts = {n: c["effort"] for n, c in suite["candidates"].items()}
            problems.append(f"{sname}: reasoning effort not matched ({efforts})")
        j = suite.get("judge") or {}
        judges.add((j.get("provider"), j.get("model"), j.get("effort")))
    if len(judges) > 1:
        problems.append(f"suites were judged by different judges: {sorted(map(str, judges))}")
    problems += _coverage_problems(data, min_coverage)

    agreement = data.get("agreement")
    if require_second_judge and not agreement:
        problems.append("second-judge agreement required but not available")
    for sname, ag in (agreement or {}).items():
        if ag["coverage"] < min_coverage:
            problems.append(f"{sname}: second judge covered only {ag['coverage']:.0%} of answers (need {min_coverage:.0%})")
        jp, js = ag.get("judge_primary") or {}, ag.get("judge_second") or {}
        if jp.get("provider") == js.get("provider"):
            problems.append(f"{sname}: the two judges are the same vendor ({jp.get('provider')}), so it isn't a cross-check")

    blob = json.dumps(data, ensure_ascii=False)
    for probe in private_probes:
        if probe and json.dumps(probe, ensure_ascii=False)[1:-1] in blob:  # compare in JSON-escaped form
            problems.append("held-out ticket text or id appears in the public data")
            break
    return problems


def assert_publishable(data: dict, **kwargs) -> None:
    problems = check_gates(data, **kwargs)
    if problems:
        raise GateError(problems)
