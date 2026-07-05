"""Report generation — aggregates verdicts into a Markdown comparison report
and a machine-readable JSON dump.

Dimensions are discovered from the verdicts (so any use case's rubric renders
without changes here). When a `scorecard` config is supplied, candidates are
ranked by a balanced composite of quality + latency + cost; otherwise by
quality alone.
"""
from __future__ import annotations

import json
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


def _aggregate(results: list[TaskResult], pricing: dict) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"quality": [], "latency": [], "cost": [], "out_tokens": [], "model": "", "errors": 0}
    )
    for tr in results:
        for r in tr.results:
            e = agg[r.candidate]
            e["model"] = r.model
            if r.error:
                e["errors"] += 1
                continue
            e["latency"].append(r.latency_s)
            e["out_tokens"].append(r.output_tokens)
            price = pricing.get(r.candidate)  # [in_per_1M, out_per_1M] or None
            if price:
                e["cost"].append((r.input_tokens * price[0] + r.output_tokens * price[1]) / 1e6)
            else:
                e["cost"].append(0.0)  # flat-rate / subscription
            if r.verdict and not r.verdict.parse_error:
                e["quality"].append(r.verdict.overall)
    return agg


def _scorecard_rows(agg: dict[str, dict], weights: dict) -> list[tuple[str, dict]]:
    """Attach normalized quality/latency/cost + a weighted composite to each
    candidate, then rank by the composite (higher is better)."""
    wq, wl, wc = weights.get("quality", 1), weights.get("latency", 0), weights.get("cost", 0)
    wsum = (wq + wl + wc) or 1
    wq, wl, wc = wq / wsum, wl / wsum, wc / wsum

    lat = {n: _avg(e["latency"]) for n, e in agg.items()}
    cost = {n: _avg(e["cost"]) for n, e in agg.items()}

    def inv_minmax(values: dict[str, float]) -> dict[str, float]:
        # Lower is better (latency, cost) -> map best to 1.0, worst to 0.0.
        lo, hi = min(values.values()), max(values.values())
        if hi == lo:
            return {n: 1.0 for n in values}
        return {n: 1 - (v - lo) / (hi - lo) for n, v in values.items()}

    l_norm, c_norm = inv_minmax(lat), inv_minmax(cost)

    rows = []
    for n, e in agg.items():
        q = _avg(e["quality"])
        q_norm = (q - 1) / 4 if q else 0.0  # 1-5 quality -> 0..1 absolute
        composite = wq * q_norm + wl * l_norm[n] + wc * c_norm[n]
        rows.append((n, {**e, "q": q, "lat": lat[n], "cost_avg": cost[n], "composite": composite}))
    rows.sort(key=lambda kv: kv[1]["composite"], reverse=True)
    return rows


def to_markdown(results: list[TaskResult], scorecard: dict | None = None) -> str:
    scorecard = scorecard or {}
    weights = scorecard.get("weights", {"quality": 1.0})
    pricing = scorecard.get("pricing", {})
    balanced = any(weights.get(k) for k in ("latency", "cost"))
    dims = _dimensions(results)

    lines = [
        "# Multi-Provider Model Evaluation Report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    agg = _aggregate(results, pricing)
    rows = _scorecard_rows(agg, weights)

    if balanced:
        w = {k: weights.get(k, 0) for k in ("quality", "latency", "cost")}
        lines += [
            "## Balanced scorecard",
            "",
            f"Composite = quality×{w['quality']} + latency×{w['latency']} + cost×{w['cost']} "
            "(each normalized 0–1; latency & cost inverted so lower is better).",
            "",
            "| Rank | Candidate | Model | Composite | Quality (1-5) | Avg latency | Avg cost/task | Errors |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for rank, (name, e) in enumerate(rows, 1):
            cost_cell = f"${e['cost_avg']:.5f}" if e["cost_avg"] else "flat-rate"
            lines.append(
                f"| {rank} | {name} | `{e['model']}` | **{e['composite']:.3f}** "
                f"| {e['q']:.2f} | {e['lat']:.1f}s | {cost_cell} | {e['errors']} |"
            )
    else:
        lines += [
            "## Leaderboard",
            "",
            "| Rank | Candidate | Model | Avg score (1-5) | Avg latency | Avg output tokens | Errors |",
            "|---|---|---|---|---|---|---|",
        ]
        for rank, (name, e) in enumerate(rows, 1):
            lines.append(
                f"| {rank} | {name} | `{e['model']}` | {e['q']:.2f} "
                f"| {e['lat']:.1f}s | {_avg(e['out_tokens']):.0f} | {e['errors']} |"
            )

    # ---- per-task detail ---------------------------------------------
    lines += ["", "## Per-task results", ""]
    for tr in results:
        lines += [f"### {tr.task.id} — {tr.task.category}", "", f"> {tr.task.prompt[:220]}", ""]
        header = "| Candidate | " + " | ".join(d.replace("_", " ") for d in dims) + " | Overall | Notes |"
        lines += [header, "|" + "---|" * (len(dims) + 3)]
        for r in sorted(tr.results, key=lambda x: (x.verdict.overall if x.verdict else 0), reverse=True):
            if r.error:
                lines.append(f"| {r.candidate} | " + "— | " * len(dims) + f"— | ERROR: {r.error[:80]} |")
                continue
            v = r.verdict
            if v is None or v.parse_error:
                note = f"judge parse error: {v.parse_error[:60]}" if v else "not judged"
                lines.append(f"| {r.candidate} | " + "— | " * len(dims) + f"— | {note} |")
                continue
            cells = " | ".join(str(v.scores.get(d, "—")) for d in dims)
            lines.append(f"| {r.candidate} | {cells} | **{v.overall}** | {v.rationale[:120]} |")
        lines.append("")

    return "\n".join(lines)
