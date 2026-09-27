"""scripts/compare_judges.py refuses to compare runs with missing verdicts."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _summary(path, rates):
    cands = {c: {"quality_mean": 4.5, "critical_violations": 0, "judge_failure_rate": r} for c, r in rates.items()}
    path.write_text(json.dumps({"ranking": list(rates), "candidates": cands}))
    return path


def _run(a, b):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "compare_judges.py"), str(a), str(b)],
                          capture_output=True, text=True, timeout=60)


def test_partial_run_is_invalid(tmp_path):
    a = _summary(tmp_path / "a.json", {"x": 0.0, "y": 0.0})
    b = _summary(tmp_path / "b.json", {"x": 0.65, "y": 0.0})
    proc = _run(a, b)
    assert proc.returncode == 2
    assert "INVALID: run B left 65% of x's verdicts unjudged" in proc.stdout
    assert "Spearman" not in proc.stdout


def test_complete_runs_compare(tmp_path):
    a = _summary(tmp_path / "a.json", {"x": 0.04, "y": 0.0})
    b = _summary(tmp_path / "b.json", {"x": 0.0, "y": 0.02})
    proc = _run(a, b)
    assert proc.returncode == 0 and "Spearman rho): 1.00" in proc.stdout
