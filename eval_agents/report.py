"""Report generation — aggregates verdicts into a Markdown comparison report,
a machine-readable summary (for regression gating), and a full JSON dump.

Dimensions are discovered from the verdicts (so any use case's rubric renders
without changes here). When a `scorecard` config is supplied, candidates are
ranked by a balanced composite of quality + latency + cost; otherwise by
quality alone. Guardrail flags are counted as hard events, separate from the
1-5 quality averages.
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


def _pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))]


def summarize(results: list[TaskResult], scorecard: dict | None = None) -> dict:
    """Per-candidate stats + composite ranking. The single source of truth
    consumed by the markdown report, summary.json, and --baseline gating."""
    scorecard = scorecard or {}
    weights = scorecard.get("weights", {"quality": 1.0})
    pricing = scorecard.get("pricing", {})

    raw: dict[str, dict] = defaultdict(
        lambda: {
            "model": "", "quality": [], "latency": [], "in_tokens": [], "out_tokens": [],
            "in_cost": [], "out_cost": [], "violations": 0, "flags": defaultdict(int), "errors": 0,
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
                if not r.verdict.parse_error:
                    e["quality"].append(r.verdict.overall)
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

    stats: dict[str, dict] = {}
    for name, e in raw.items():
        cost_task = _avg(e["in_cost"]) + _avg(e["out_cost"])
        stats[name] = {
            "model": e["model"],
            "n_samples": len(e["latency"]),
            "quality_mean": round(_avg(e["quality"]), 3),
            "quality_sd": round(_sd(e["quality"]), 3),
            "latency_p50": round(_pct(e["latency"], 0.50), 2),
            "latency_p95": round(_pct(e["latency"], 0.95), 2),
            "latency_mean": round(_avg(e["latency"]), 2),
            "input_tokens_avg": round(_avg(e["in_tokens"])),
            "output_tokens_avg": round(_avg(e["out_tokens"])),
            "input_cost_avg": round(_avg(e["in_cost"]), 6),
            "output_cost_avg": round(_avg(e["out_cost"]), 6),
            "cost_per_task": round(cost_task, 6),
            "priced": bool(pricing.get(name)),
            "critical_violations": e["violations"],
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

    ranking = sorted(stats, key=lambda n: stats[n]["composite"], reverse=True)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weights": {"quality": wq, "latency": wl, "cost": wc},
        "monthly_volume": scorecard.get("monthly_volume"),
        "ranking": ranking,
        "candidates": stats,
    }


def to_summary_json(results: list[TaskResult], scorecard: dict | None = None) -> str:
    return json.dumps(summarize(results, scorecard), indent=2)


def to_markdown(results: list[TaskResult], scorecard: dict | None = None) -> str:
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

    # ---- scorecard / leaderboard --------------------------------------
    if balanced:
        w = summary["weights"]
        lines += [
            "## Balanced scorecard",
            "",
            f"Composite = quality×{w['quality']:.2f} + latency×{w['latency']:.2f} + "
            f"cost×{w['cost']:.2f} (each normalized 0–1; latency & cost inverted).",
            "**Critical violations are a launch gate, not a weighted score — treat any "
            "non-zero count as disqualifying regardless of rank.**",
            "",
            "| Rank | Candidate | Model | Composite | Quality (1-5) | ⚠ Violations | Latency p50/p95 | Cost/task | Errors |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for rank, name in enumerate(summary["ranking"], 1):
            s = stats[name]
            q = f"{s['quality_mean']:.2f}"
            if s["quality_sd"]:
                q += f" ± {s['quality_sd']:.2f}"
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
            "| Rank | Candidate | Model | Quality (1-5) | ⚠ Violations | Latency p50/p95 | Avg output tokens | Errors |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for rank, name in enumerate(summary["ranking"], 1):
            s = stats[name]
            q = f"{s['quality_mean']:.2f}"
            if s["quality_sd"]:
                q += f" ± {s['quality_sd']:.2f}"
            lines.append(
                f"| {rank} | {name} | `{s['model']}` | {q} | {s['critical_violations']} "
                f"| {s['latency_p50']:.1f}s / {s['latency_p95']:.1f}s | {s['output_tokens_avg']} | {s['errors']} |"
            )

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
