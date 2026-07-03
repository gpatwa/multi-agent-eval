"""Report generation — aggregates verdicts into a Markdown comparison
report and a machine-readable JSON dump."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone

from .judge import DIMENSIONS
from .runner import TaskResult


def to_json(results: list[TaskResult]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2, default=str)


def to_markdown(results: list[TaskResult]) -> str:
    lines = [
        "# Multi-Provider Model Evaluation Report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    # ---- leaderboard -------------------------------------------------
    agg: dict[str, dict] = defaultdict(
        lambda: {"overall": [], "latency": [], "out_tokens": [], "model": "", "errors": 0}
    )
    for tr in results:
        for r in tr.results:
            entry = agg[r.candidate]
            entry["model"] = r.model
            if r.error:
                entry["errors"] += 1
                continue
            entry["latency"].append(r.latency_s)
            entry["out_tokens"].append(r.output_tokens)
            if r.verdict and not r.verdict.parse_error:
                entry["overall"].append(r.verdict.overall)

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    ranked = sorted(agg.items(), key=lambda kv: avg(kv[1]["overall"]), reverse=True)

    lines += [
        "## Leaderboard",
        "",
        "| Rank | Candidate | Model | Avg score (1-5) | Avg latency | Avg output tokens | Errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for rank, (name, e) in enumerate(ranked, 1):
        lines.append(
            f"| {rank} | {name} | `{e['model']}` | {avg(e['overall']):.2f} "
            f"| {avg(e['latency']):.1f}s | {avg(e['out_tokens']):.0f} | {e['errors']} |"
        )

    # ---- per-task detail ---------------------------------------------
    lines += ["", "## Per-task results", ""]
    for tr in results:
        lines += [f"### {tr.task.id} — {tr.task.category}", "", f"> {tr.task.prompt[:200]}", ""]
        header = "| Candidate | " + " | ".join(d.replace("_", " ") for d in DIMENSIONS) + " | Overall | Notes |"
        lines += [header, "|" + "---|" * (len(DIMENSIONS) + 3)]
        for r in sorted(tr.results, key=lambda x: (x.verdict.overall if x.verdict else 0), reverse=True):
            if r.error:
                lines.append(f"| {r.candidate} | " + "— | " * len(DIMENSIONS) + f"— | ERROR: {r.error[:80]} |")
                continue
            v = r.verdict
            if v is None or v.parse_error:
                note = f"judge parse error: {v.parse_error[:60]}" if v else "not judged"
                lines.append(f"| {r.candidate} | " + "— | " * len(DIMENSIONS) + f"— | {note} |")
                continue
            cells = " | ".join(str(v.scores[d]) for d in DIMENSIONS)
            lines.append(f"| {r.candidate} | {cells} | **{v.overall}** | {v.rationale[:100]} |")
        lines.append("")

    return "\n".join(lines)
