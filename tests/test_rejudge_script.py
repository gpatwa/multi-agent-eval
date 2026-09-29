"""scripts/rejudge.py: re-scores stored answers without re-running candidates."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def test_rejudge_keeps_answers_and_recomputes_verdicts(tmp_path):
    src, out = tmp_path / "src", tmp_path / "rejudged"
    proc = _run(str(ROOT / "main.py"), "--config", "config.triage.demo.yaml", "--out", str(src))
    assert proc.returncode == 0, proc.stderr
    proc = _run(str(ROOT / "scripts" / "rejudge.py"), str(src), "--config", "config.triage.demo.yaml", "--out", str(out))
    assert proc.returncode == 0, proc.stderr

    before = json.loads((src / "results.json").read_text())
    after = json.loads((out / "results.json").read_text())
    flat = lambda d: [(tr["task"]["id"], r["candidate"], r["answer"], r["latency_s"], r["input_tokens"]) for tr in d for r in tr["results"]]
    assert flat(before) == flat(after)  # candidates untouched
    # mock judge is deterministic, so the same judge reproduces the same verdicts
    verdicts = lambda d: [r["verdict"]["overall"] for tr in d for r in tr["results"]]
    assert verdicts(before) == verdicts(after)
    assert (out / "summary.json").is_file() and (out / "report.md").is_file()
    # candidate settings carry over from the source run; the judge is the re-judging one
    src_settings = json.loads((src / "summary.json").read_text())["run_settings"]
    new_settings = json.loads((out / "summary.json").read_text())["run_settings"]
    assert new_settings["candidates"] == src_settings["candidates"]
    assert new_settings["judge"]["model"] == "mock-judge" and new_settings["rejudged_from"] == str(src)
    assert "Reasoning effort" in (out / "report.md").read_text()
    assert "judge: mock/mock-judge" in (out / "rejudged_from.txt").read_text()
