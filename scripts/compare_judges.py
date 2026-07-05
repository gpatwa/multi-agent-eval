#!/usr/bin/env python3
"""Compare two runs of the same benchmark under different judges.

    python scripts/compare_judges.py results-triage/summary.json results-triage-hermes/summary.json

Where the two judges agree, trust the ranking. Where they disagree, the tool
tells you exactly which candidates to investigate by reading transcripts.

Notes:
- routing/priority are graded deterministically, so quality differences
  between runs come from the judged reply dimensions (and sampling noise).
- rank correlation is Spearman's rho over candidates present in both runs.
"""
from __future__ import annotations

import json
import sys

QUALITY_DISAGREEMENT = 0.5  # judged-quality delta worth reading transcripts for


def _spearman(order_a: list[str], order_b: list[str]) -> float:
    common = [c for c in order_a if c in order_b]
    n = len(common)
    if n < 2:
        return float("nan")
    ra = {c: i for i, c in enumerate(c for c in order_a if c in common)}
    rb = {c: i for i, c in enumerate(c for c in order_b if c in common)}
    d2 = sum((ra[c] - rb[c]) ** 2 for c in common)
    return 1 - (6 * d2) / (n * (n**2 - 1))


def main(path_a: str, path_b: str) -> None:
    a, b = json.load(open(path_a)), json.load(open(path_b))
    ca, cb = a["candidates"], b["candidates"]
    common = [c for c in a["ranking"] if c in cb]
    if not common:
        sys.exit("No common candidates between the two runs.")

    print(f"A: {path_a}\nB: {path_b}\n")
    print(f"{'candidate':<12} {'rank A':>6} {'rank B':>6} {'qual A':>7} {'qual B':>7} {'Δqual':>6}  verdict")
    disagreements = []
    for c in common:
        rank_a = a["ranking"].index(c) + 1
        rank_b = b["ranking"].index(c) + 1
        qa, qb = ca[c]["quality_mean"], cb[c]["quality_mean"]
        dq = qb - qa
        verdict = "agree"
        if abs(dq) > QUALITY_DISAGREEMENT or abs(rank_a - rank_b) > 1:
            verdict = "DISAGREE — read transcripts"
            disagreements.append(c)
        print(f"{c:<12} {rank_a:>6} {rank_b:>6} {qa:>7.2f} {qb:>7.2f} {dq:>+6.2f}  {verdict}")

    rho = _spearman(a["ranking"], b["ranking"])
    print(f"\nRank correlation (Spearman rho): {rho:.2f}"
          f"  ({'strong agreement' if rho >= 0.8 else 'moderate' if rho >= 0.5 else 'weak — judges see different things'})")

    va = {c: ca[c]["critical_violations"] for c in common}
    vb = {c: cb[c]["critical_violations"] for c in common}
    if va != vb:
        print("\nViolation-count differences (judge-flagged; deterministic PII flags should match):")
        for c in common:
            if va[c] != vb[c]:
                print(f"  {c}: {va[c]} (A) vs {vb[c]} (B)")
                if c not in disagreements:
                    disagreements.append(c)  # violations are launch gates — always investigate

    if disagreements:
        print(f"\nNext step: in each run's report.md, read the per-task rows for: {', '.join(disagreements)}")
    else:
        print("\nJudges agree — the ranking is defensible.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
