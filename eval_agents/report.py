"""Report generation — aggregates verdicts into a Markdown comparison report,
a machine-readable summary (for regression gating), and a full JSON dump.

Dimensions are discovered from the verdicts (so any use case's rubric renders
without changes here). When a `scorecard` config is supplied, candidates are
ranked by a balanced composite of quality + latency + cost; otherwise by
quality alone. Guardrail flags are counted as hard events, separate from the
1-5 quality averages.

Judge spend is reported separately from candidate cost: it's evaluation
overhead (the same judge scores every candidate), so it never feeds the
composite ranking or the --baseline gate — it answers "what does running
this eval cost?", priced via `scorecard.judge_pricing: [in, out]`.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone

from .runner import TaskResult


def to_json(results: list[TaskResult]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2, default=str)


# Share of unjudged verdicts above which a run's quality/ranking shouldn't be trusted.
JUDGE_FAILURE_WARN = 0.10


def _dimensions(results: list[TaskResult]) -> list[str]:
    """Ordered union of score keys across all verdicts (rubric-agnostic)."""
    seen: list[str] = []
    for tr in results:
        for r in tr.results:
            if r.verdict and r.verdict.scores:
                for k in r.verdict.scores:
                    if k not in seen:
                        seen.append(k)
    return seen


def _avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _sd(xs):
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


# Two-sided 95% Student-t critical values by degrees of freedom; 1.96 beyond.
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26,
        10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06, 30: 2.04, 40: 2.02, 60: 2.00}


def _ci95(per_task_means: list[float]) -> float:
    """Half-width of a 95% t-interval for the mean, treating each TASK as one
    sample (trials are averaged within a task first — repeat trials of the
    same ticket aren't independent evidence, so they must not narrow the CI)."""
    n = len(per_task_means)
    if n < 2:
        return 0.0
    df = n - 1
    t = next((v for k, v in sorted(_T95.items()) if df <= k), 1.96)
    return t * statistics.stdev(per_task_means) / n ** 0.5


def _pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))]


def rank(stats: dict) -> list[str]:
    """Candidate names, best first. A guardrail violation is a launch gate, so any candidate with one
    ranks below every clean candidate; within each group the order is by composite."""
    return sorted(stats, key=lambda n: (stats[n]["critical_violations"] > 0, -stats[n]["composite"]))


def summarize(results: list[TaskResult], scorecard: dict | None = None) -> dict:
    """Per-candidate stats + composite ranking. The single source of truth
    consumed by the markdown report, summary.json, and --baseline gating."""
    scorecard = scorecard or {}
    weights = scorecard.get("weights", {"quality": 1.0})
    pricing = scorecard.get("pricing", {})
    judge_price = scorecard.get("judge_pricing")  # [in_per_1M, out_per_1M] or None

    raw: dict[str, dict] = defaultdict(
        lambda: {
            "model": "", "quality": [], "latency": [], "in_tokens": [], "out_tokens": [],
            "in_cost": [], "out_cost": [], "violations": 0, "flags": defaultdict(int), "errors": 0,
            "judge_in": 0, "judge_out": 0, "judge_failures": 0, "judged": 0, "quality_by_task": defaultdict(list), "passed": [], "passed_by_task": defaultdict(list),
        }
    )
    for tr in results:
        for r in tr.results:
            e = raw[r.candidate]
            e["model"] = r.model
            if r.error:
                e["errors"] += 1
                continue
            e["latency"].append(r.latency_s)
            e["in_tokens"].append(r.input_tokens)
            e["out_tokens"].append(r.output_tokens)
            price = pricing.get(r.candidate)  # [in_per_1M, out_per_1M] or None
            e["in_cost"].append(r.input_tokens * price[0] / 1e6 if price else 0.0)
            e["out_cost"].append(r.output_tokens * price[1] / 1e6 if price else 0.0)
            if r.verdict:
                e["judged"] += 1
                if r.verdict.parse_error:
                    e["judge_failures"] += 1
                e["judge_in"] += r.verdict.judge_input_tokens
                e["judge_out"] += r.verdict.judge_output_tokens
                if not r.verdict.parse_error:
                    e["quality"].append(r.verdict.overall)
                    e["quality_by_task"][tr.task.id].append(r.verdict.overall)
                    if r.verdict.passed is not None:
                        e["passed"].append(r.verdict.passed)
                        e["passed_by_task"][tr.task.id].append(r.verdict.passed)
                if r.verdict.flags:
                    e["violations"] += 1
                    for f in r.verdict.flags:
                        e["flags"][f] += 1

    # normalize weights
    wq = weights.get("quality", 1) or 0
    wl = weights.get("latency", 0) or 0
    wc = weights.get("cost", 0) or 0
    wsum = (wq + wl + wc) or 1
    wq, wl, wc = wq / wsum, wl / wsum, wc / wsum

    def judge_cost(tok_in: int, tok_out: int) -> float:
        return (tok_in * judge_price[0] + tok_out * judge_price[1]) / 1e6 if judge_price else 0.0

    stats: dict[str, dict] = {}
    for name, e in raw.items():
        cost_task = _avg(e["in_cost"]) + _avg(e["out_cost"])
        n = len(e["latency"])
        stats[name] = {
            "model": e["model"],
            "n_samples": len(e["latency"]),
            "quality_mean": round(_avg(e["quality"]), 3),
            "quality_sd": round(_sd(e["quality"]), 3),
            "n_tasks_scored": len(e["quality_by_task"]),
            # strict pass rate, only for rubrics with pass/fail (else None)
            "pass_rate": round(sum(e["passed"]) / len(e["passed"]), 4) if e["passed"] else None,
            # with repeated trials: share of tasks passed on EVERY trial (a
            # consistency floor; pass_rate alone hides flaky passes)
            "pass_all_trials": (
                round(sum(all(v) for v in e["passed_by_task"].values()) / len(e["passed_by_task"]), 4)
                if any(len(v) > 1 for v in e["passed_by_task"].values()) else None
            ),
            "quality_ci95": round(_ci95([_avg(v) for v in e["quality_by_task"].values()]), 3),
            "latency_p50": round(_pct(e["latency"], 0.50), 2),
            "latency_p95": round(_pct(e["latency"], 0.95), 2),
            "latency_mean": round(_avg(e["latency"]), 2),
            "input_tokens_avg": round(_avg(e["in_tokens"])),
            "output_tokens_avg": round(_avg(e["out_tokens"])),
            "input_cost_avg": round(_avg(e["in_cost"]), 6),
            "output_cost_avg": round(_avg(e["out_cost"]), 6),
            "cost_per_task": round(cost_task, 6),
            "candidate_cost_total": round(sum(e["in_cost"]) + sum(e["out_cost"]), 6),
            "judge_input_tokens_total": e["judge_in"],
            "judge_output_tokens_total": e["judge_out"],
            "judge_cost_total": round(judge_cost(e["judge_in"], e["judge_out"]), 6),
            "judge_cost_per_task": round(judge_cost(e["judge_in"], e["judge_out"]) / n, 6) if n else 0.0,
            "priced": bool(pricing.get(name)),
            "critical_violations": e["violations"],
            # verdicts the judge failed to produce (transport/quota errors or
            # unparseable output) — excluded from quality, so a high count
            # means quality_mean rests on a subset of the answers
            "judge_failures": e["judge_failures"],
            "judge_failure_rate": round(e["judge_failures"] / e["judged"], 4) if e["judged"] else 0.0,
            "flag_counts": dict(e["flags"]),
            "errors": e["errors"],
        }

    # composite: quality absolute (1-5 -> 0..1); latency/cost min-max inverted
    def inv_minmax(key):
        vals = {n: s[key] for n, s in stats.items()}
        lo, hi = min(vals.values(), default=0), max(vals.values(), default=0)
        if hi == lo:
            return {n: 1.0 for n in vals}
        return {n: 1 - (v - lo) / (hi - lo) for n, v in vals.items()}

    l_norm, c_norm = inv_minmax("latency_mean"), inv_minmax("cost_per_task")
    for name, s in stats.items():
        q_norm = (s["quality_mean"] - 1) / 4 if s["quality_mean"] else 0.0
        s["composite"] = round(wq * q_norm + wl * l_norm[name] + wc * c_norm[name], 4)

    ranking = rank(stats)
    judge_in = sum(e["judge_in"] for e in raw.values())
    judge_out = sum(e["judge_out"] for e in raw.values())
    candidate_total = sum(s["candidate_cost_total"] for s in stats.values())
    judge_total = judge_cost(judge_in, judge_out)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": {"quality": wq, "latency": wl, "cost": wc},
        "monthly_volume": scorecard.get("monthly_volume"),
        "ranking": ranking,
        "candidates": stats,
        "evaluation_cost": {
            "judge_priced": bool(judge_price),
            "judge_input_tokens": judge_in,
            "judge_output_tokens": judge_out,
            "judge_cost_total": round(judge_total, 6),
            "candidate_cost_total": round(candidate_total, 6),
            "run_cost_total": round(candidate_total + judge_total, 6),
        },
    }


def to_summary_json(results: list[TaskResult], scorecard: dict | None = None, settings: dict | None = None) -> str:
    summary = summarize(results, scorecard)
    if settings:
        summary["run_settings"] = settings
    return json.dumps(summary, indent=2)


def _settings_lines(settings: dict) -> list[str]:
    """Report header: exactly what ran, and whether effort was matched."""
    efforts = settings["effort_by_candidate"]
    if settings["effort_matched"]:
        head = f"**Reasoning effort: matched — `{next(iter(efforts.values()))}` for every candidate.**"
    else:
        listed = ", ".join(f"{n}: {e}" for n, e in efforts.items())
        head = (f"**⚠ Reasoning effort NOT matched** ({listed}). Latency and quality differences may "
                "reflect effort settings, not the models.")
    lines = [head, "", "| Candidate | Model | Effort | Notes |", "|---|---|---|---|"]
    for name, info in settings["candidates"].items():
        notes = []
        if info.get("cli_version"):
            notes.append(info["cli_version"])
        tier = (info.get("user_config") or {}).get("service_tier")
        if tier:
            notes.append(f"personal Codex config: service_tier={tier}")
        lines.append(f"| {name} | `{info['model']}` | {info['effort']} | {'; '.join(notes) or '—'} |")
    j = settings["judge"]
    lines += ["", f"Judge: `{j['model']}` ({j['provider']}), effort {j['effort']}"
              + (f", {j['cli_version']}" if j.get("cli_version") else ""), ""]
    return lines


def _quality_cell(s: dict) -> str:
    q = f"{s['quality_mean']:.2f}"
    if s["quality_ci95"]:
        q += f" ± {s['quality_ci95']:.2f}"
    return q


def to_markdown(results: list[TaskResult], scorecard: dict | None = None, settings: dict | None = None) -> str:
    scorecard = scorecard or {}
    summary = summarize(results, scorecard)
    stats = summary["candidates"]
    balanced = summary["weights"]["latency"] > 0 or summary["weights"]["cost"] > 0
    any_priced = any(s["priced"] for s in stats.values())
    volume = summary.get("monthly_volume")
    dims = _dimensions(results)

    lines = [
        "# Multi-Provider Model Evaluation Report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    if settings:
        lines += _settings_lines(settings)

    # ---- judge coverage warning ----------------------------------------
    failed = {n: s for n, s in stats.items() if s["judge_failures"]}
    if failed:
        worst = max(s["judge_failure_rate"] for s in failed.values())
        lines += [
            f"> **{'⚠ ' if worst > JUDGE_FAILURE_WARN else ''}Judge coverage:** "
            + ", ".join(f"{n} {s['judge_failures']} unjudged ({s['judge_failure_rate']:.0%})" for n, s in failed.items())
            + ". Unjudged answers are excluded from quality"
            + (" — above the {:.0%} threshold, so quality and ranking rest on a subset; "
               "re-judge before trusting them.".format(JUDGE_FAILURE_WARN) if worst > JUDGE_FAILURE_WARN else ".")
            ,
            "",
        ]

    # ---- scorecard / leaderboard --------------------------------------
    if balanced:
        w = summary["weights"]
        lines += [
            "## Balanced scorecard",
            "",
            f"Composite = quality×{w['quality']:.2f} + latency×{w['latency']:.2f} + "
            f"cost×{w['cost']:.2f} (each normalized 0–1; latency & cost inverted).",
            "**Critical violations are a launch gate, not a weighted score — treat any "
            "non-zero count as disqualifying. Any candidate with one is ranked below every clean candidate.**",
            "",
            "| Rank | Candidate | Model | Composite | Quality (1-5, ±95% CI) | ⚠ Violations | Latency p50/p95 | Cost/task | Errors |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for rank, name in enumerate(summary["ranking"], 1):
            s = stats[name]
            q = _quality_cell(s)
            cost = f"${s['cost_per_task']:.5f}" if s["priced"] else "flat-rate"
            viol = f"**{s['critical_violations']}**" if s["critical_violations"] else "0"
            lines.append(
                f"| {rank} | {name} | `{s['model']}` | **{s['composite']:.3f}** | {q} "
                f"| {viol} | {s['latency_p50']:.1f}s / {s['latency_p95']:.1f}s | {cost} | {s['errors']} |"
            )
    else:
        lines += [
            "## Leaderboard",
            "",
            "| Rank | Candidate | Model | Quality (1-5, ±95% CI) | ⚠ Violations | Latency p50/p95 | Avg output tokens | Errors |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for rank, name in enumerate(summary["ranking"], 1):
            s = stats[name]
            q = _quality_cell(s)
            lines.append(
                f"| {rank} | {name} | `{s['model']}` | {q} | {s['critical_violations']} "
                f"| {s['latency_p50']:.1f}s / {s['latency_p95']:.1f}s | {s['output_tokens_avg']} | {s['errors']} |"
            )

    # ---- strict pass rate (rubrics with pass/fail, e.g. AutomationBench) --
    if any(s["pass_rate"] is not None for s in stats.values()):
        lines += [
            "",
            "## Pass rate",
            "",
            "A task passes only if every assertion passes (AutomationBench's official "
            "metric); quality above is the partial credit.",
            "",
        ]
        multi = any(s["pass_all_trials"] is not None for s in stats.values())
        if multi:
            lines += [
                "With repeated trials, *pass rate* counts every run; *passed every trial* is the share "
                "of tasks that passed on all of them.",
                "",
                "| Candidate | Pass rate | Passed every trial | Tasks |",
                "|---|---|---|---|",
            ]
        else:
            lines += ["| Candidate | Pass rate | Tasks |", "|---|---|---|"]
        for name in summary["ranking"]:
            s = stats[name]
            if s["pass_rate"] is None:
                continue
            every = f" {s['pass_all_trials']:.1%} |" if s["pass_all_trials"] is not None else (" — |" if multi else "")
            lines.append(f"| {name} | {s['pass_rate']:.1%} |{every} {s['n_tasks_scored']} |")

    # ---- guardrail flag breakdown --------------------------------------
    if any(s["critical_violations"] for s in stats.values()):
        lines += ["", "## Guardrail violations", "", "| Candidate | Flag | Count |", "|---|---|---|"]
        for name in summary["ranking"]:
            for flag, count in sorted(stats[name]["flag_counts"].items()):
                lines.append(f"| {name} | `{flag}` | {count} |")

    # ---- cost detail ----------------------------------------------------
    if any_priced:
        header = "| Candidate | Avg in tokens | Avg out tokens | In cost | Out cost | Cost/task |"
        sep = "|---|---|---|---|---|---|"
        if volume:
            header += f" Projected @ {volume:,}/mo |"
            sep += "---|"
        lines += ["", "## Cost detail", "", header, sep]
        for name in summary["ranking"]:
            s = stats[name]
            if not s["priced"]:
                row = f"| {name} | {s['input_tokens_avg']} | {s['output_tokens_avg']} | flat-rate | flat-rate | flat-rate |"
                if volume:
                    row += " — |"
                lines.append(row)
                continue
            row = (
                f"| {name} | {s['input_tokens_avg']} | {s['output_tokens_avg']} "
                f"| ${s['input_cost_avg']:.5f} | ${s['output_cost_avg']:.5f} | ${s['cost_per_task']:.5f} |"
            )
            if volume:
                row += f" ${s['cost_per_task'] * volume:,.2f} |"
            lines.append(row)

    # ---- evaluation (run) cost -------------------------------------------
    ev = summary["evaluation_cost"]
    if ev["judge_input_tokens"] or ev["judge_output_tokens"] or any_priced:
        judge_cost_cell = f"${ev['judge_cost_total']:.4f}" if ev["judge_priced"] else "flat-rate / unpriced"
        lines += [
            "",
            "## Evaluation cost",
            "",
            "What this run cost end to end. Judge spend is evaluation overhead and "
            "is not part of any candidate's cost/task or the composite.",
            "",
            "| Candidate | Judge in tokens | Judge out tokens | Judge cost | Candidate cost |",
            "|---|---|---|---|---|",
        ]
        for name in summary["ranking"]:
            s = stats[name]
            jc = f"${s['judge_cost_total']:.4f}" if ev["judge_priced"] else "—"
            cc = f"${s['candidate_cost_total']:.4f}" if s["priced"] else "flat-rate"
            lines.append(
                f"| {name} | {s['judge_input_tokens_total']:,} | {s['judge_output_tokens_total']:,} | {jc} | {cc} |"
            )
        lines += [
            "",
            f"**Judge total:** {ev['judge_input_tokens']:,} in / {ev['judge_output_tokens']:,} out tokens, "
            f"{judge_cost_cell}. **Run total (priced parts):** ${ev['run_cost_total']:.4f}.",
        ]

    # ---- per-task detail ------------------------------------------------
    multi_trial = any(r.trial > 0 for tr in results for r in tr.results)
    lines += ["", "## Per-task results", ""]
    for tr in results:
        lines += [f"### {tr.task.id} — {tr.task.category}", "", f"> {tr.task.prompt[:220]}", ""]
        header = "| Candidate | " + " | ".join(d.replace("_", " ") for d in dims) + " | Overall | Notes |"
        lines += [header, "|" + "---|" * (len(dims) + 3)]
        for r in sorted(tr.results, key=lambda x: (x.verdict.overall if x.verdict else 0), reverse=True):
            label = f"{r.candidate} (t{r.trial + 1})" if multi_trial else r.candidate
            if r.error:
                lines.append(f"| {label} | " + "— | " * len(dims) + f"— | ERROR: {r.error[:80]} |")
                continue
            v = r.verdict
            if v is None or v.parse_error:
                note = f"judge parse error: {v.parse_error[:60]}" if v else "not judged"
                lines.append(f"| {label} | " + "— | " * len(dims) + f"— | {note} |")
                continue
            cells = " | ".join(str(v.scores.get(d, "—")) for d in dims)
            lines.append(f"| {label} | {cells} | **{v.overall}** | {v.rationale[:120]} |")
        lines.append("")

    return "\n".join(lines)
