"""Read a finished run directory back into TaskResult objects.

Shared by the tools that post-process runs (scripts/rejudge.py re-scores stored
answers, scripts/merge_runs.py combines candidates from separate runs).
"""
from __future__ import annotations

import json
import pathlib

from .judge import Verdict
from .runner import CandidateResult, Task, TaskResult


def load_settings(run_dir: pathlib.Path) -> dict | None:
    """The run settings recorded in `<run_dir>/summary.json`, if the run has them."""
    try:
        return json.loads((pathlib.Path(run_dir) / "summary.json").read_text()).get("run_settings")
    except (OSError, json.JSONDecodeError):
        return None


def load_results(path: pathlib.Path, with_verdicts: bool = False) -> list[TaskResult]:
    """Rebuild TaskResult objects from `<path>/results.json`.

    Verdicts are dropped unless `with_verdicts` (rejudging must start clean)."""
    data = json.loads((pathlib.Path(path) / "results.json").read_text())
    out = []
    for tr in data:
        results = []
        for r in tr["results"]:
            fields = {k: v for k, v in r.items() if k != "verdict"}
            cr = CandidateResult(**fields)
            if with_verdicts and r.get("verdict"):
                cr.verdict = Verdict(**r["verdict"])
            results.append(cr)
        out.append(TaskResult(task=Task(**tr["task"]), results=results))
    return out
