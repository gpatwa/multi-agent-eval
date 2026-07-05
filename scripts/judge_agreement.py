#!/usr/bin/env python3
"""Validate the LLM judge against human labels.

An LLM judge is itself a model that needs evaluating. Workflow:

1. Export a labeling sheet from a completed run:
       python scripts/judge_agreement.py export results-triage/results.json labels.csv

2. Fill in the human_* columns (1-5) for 20-30 rows yourself.

3. Score agreement:
       python scripts/judge_agreement.py score labels.csv

Reports per-dimension exact agreement, within-1 agreement, and Pearson r.
Rules of thumb: within-1 agreement >= 0.8 and r >= 0.6 means the judge is
usable; below that, fix the rubric/judge model before trusting rankings.
"""
from __future__ import annotations

import csv
import json
import math
import sys


def export(results_path: str, out_csv: str) -> None:
    data = json.load(open(results_path))
    dims: list[str] = []
    rows = []
    for tr in data:
        for r in tr["results"]:
            v = r.get("verdict")
            if not v or v.get("parse_error") or r.get("error"):
                continue
            judged = {k: s for k, s in v["scores"].items() if k not in ("routing", "priority")}
            for k in judged:
                if k not in dims:
                    dims.append(k)
            rows.append(
                {
                    "task_id": tr["task"]["id"],
                    "candidate": r["candidate"],
                    "trial": r.get("trial", 0),
                    "answer": r["answer"],
                    **{f"judge_{k}": s for k, s in judged.items()},
                }
            )
    fields = ["task_id", "candidate", "trial", "answer"]
    fields += [f"judge_{d}" for d in dims] + [f"human_{d}" for d in dims]
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out_csv}.")
    print(f"Fill in the human_* columns (1-5) for judged dimensions: {', '.join(dims)}")


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx and sy else float("nan")


def score(csv_path: str) -> None:
    rows = list(csv.DictReader(open(csv_path)))
    dims = sorted({c[len("judge_"):] for c in rows[0] if c.startswith("judge_")})

    print(f"{'dimension':<20} {'n':>4} {'exact':>7} {'within-1':>9} {'pearson r':>10}")
    any_labeled = False
    for d in dims:
        pairs = []
        for row in rows:
            j, h = row.get(f"judge_{d}", ""), row.get(f"human_{d}", "")
            if j.strip() and h.strip():
                pairs.append((float(j), float(h)))
        if not pairs:
            continue
        any_labeled = True
        n = len(pairs)
        exact = sum(1 for j, h in pairs if j == h) / n
        within1 = sum(1 for j, h in pairs if abs(j - h) <= 1) / n
        r = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
        print(f"{d:<20} {n:>4} {exact:>7.0%} {within1:>9.0%} {r:>10.2f}")

    if not any_labeled:
        sys.exit("No human_* labels filled in yet — edit the CSV first.")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "export":
        export(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "labels.csv")
    elif len(sys.argv) >= 3 and sys.argv[1] == "score":
        score(sys.argv[2])
    else:
        sys.exit(__doc__)
