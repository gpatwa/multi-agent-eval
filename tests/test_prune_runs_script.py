"""Tests for scripts/prune_runs.py's selection logic against a throwaway
temp directory — never touches the real runs/."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "prune_runs.py"
spec = importlib.util.spec_from_file_location("prune_runs", SCRIPT)
prune_runs = importlib.util.module_from_spec(spec)
sys.modules["prune_runs"] = prune_runs
spec.loader.exec_module(prune_runs)


def _make_run(runs_dir: pathlib.Path, run_id: str, age_days: float):
    d = runs_dir / run_id
    d.mkdir(parents=True)
    started_at = time.time() - age_days * 86400
    (d / "run.json").write_text(json.dumps({"started_at": started_at}))
    return d


def test_no_runs_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_runs, "RUNS_DIR", tmp_path / "does-not-exist")
    assert prune_runs.select_prune_candidates(keep_last=5, keep_days=None) == []


def test_keep_last_keeps_newest_n(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(prune_runs, "RUNS_DIR", runs_dir)
    _make_run(runs_dir, "oldest", age_days=10)
    _make_run(runs_dir, "middle", age_days=5)
    newest = _make_run(runs_dir, "newest", age_days=1)

    candidates = prune_runs.select_prune_candidates(keep_last=1, keep_days=None)
    names = {p.name for p in candidates}
    assert names == {"oldest", "middle"}
    assert newest not in candidates


def test_keep_days_keeps_recent_regardless_of_count(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(prune_runs, "RUNS_DIR", runs_dir)
    _make_run(runs_dir, "old", age_days=40)
    _make_run(runs_dir, "recent", age_days=2)

    candidates = prune_runs.select_prune_candidates(keep_last=None, keep_days=30)
    names = {p.name for p in candidates}
    assert names == {"old"}


def test_keep_last_and_keep_days_are_ored(tmp_path, monkeypatch):
    """A run survives if EITHER condition keeps it -- e.g. it's old but
    still inside the keep-last-N newest."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(prune_runs, "RUNS_DIR", runs_dir)
    _make_run(runs_dir, "very_old_but_kept_by_count", age_days=100)
    _make_run(runs_dir, "recent", age_days=1)

    candidates = prune_runs.select_prune_candidates(keep_last=2, keep_days=30)
    assert candidates == []  # both runs kept: one by count, one by age


def test_nothing_to_prune_when_all_fit(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(prune_runs, "RUNS_DIR", runs_dir)
    _make_run(runs_dir, "a", age_days=1)

    assert prune_runs.select_prune_candidates(keep_last=10, keep_days=None) == []
