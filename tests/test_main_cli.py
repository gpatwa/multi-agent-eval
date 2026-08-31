"""End-to-end smoke tests through the real CLI entrypoint, using the mock
provider so no network/credentials are needed. Exercises the full
config -> run -> report -> summary.json -> --baseline regression-gate path
in one shot — the same path a real `python main.py --config ...` takes."""
from __future__ import annotations

import json
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run_cli(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_demo_config_runs_and_writes_artifacts(tmp_path):
    out = tmp_path / "results"
    proc = _run_cli("--config", "config.demo.yaml", "--out", str(out), cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert (out / "results.json").is_file()
    assert (out / "summary.json").is_file()
    assert (out / "report.md").is_file()

    summary = json.loads((out / "summary.json").read_text())
    assert set(summary["candidates"]) == {"mock-alpha", "mock-beta", "mock-gamma"}
    assert summary["ranking"]  # non-empty


def test_baseline_regression_gate_passes_against_itself(tmp_path):
    out = tmp_path / "results"
    _run_cli("--config", "config.demo.yaml", "--out", str(out), cwd=ROOT)
    proc = _run_cli(
        "--config", "config.demo.yaml", "--out", str(tmp_path / "results2"),
        "--baseline", str(out), cwd=ROOT,
    )
    # Mock provider is deterministic per (model, prompt) hash, so a run
    # against its own baseline must never regress.
    assert proc.returncode == 0, proc.stderr
    assert "No regression" in proc.stderr


def test_baseline_regression_gate_fails_on_synthetic_drop(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "summary.json").write_text(json.dumps({
        "candidates": {"mock-alpha": {"quality_mean": 4.9, "critical_violations": 0}},
    }))

    out = tmp_path / "results"
    proc = _run_cli(
        "--config", "config.demo.yaml", "--out", str(out),
        "--baseline", str(baseline), "--regression-threshold", "0.05",
        cwd=ROOT,
    )
    assert proc.returncode == 1
    assert "REGRESSION" in proc.stderr


def test_missing_config_file_exits_nonzero(tmp_path):
    proc = _run_cli("--config", "does_not_exist.yaml", "--out", str(tmp_path / "out"), cwd=ROOT)
    assert proc.returncode != 0
