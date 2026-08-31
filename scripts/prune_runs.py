#!/usr/bin/env python3
"""Prune old web-UI run history (runs/<id>/) — the one artifact directory
this project actually manages as a growing system (see webapp/server.py's
persistence). Nothing is ever deleted automatically; this is an explicit,
opt-in command, and defaults to a dry run.

    python scripts/prune_runs.py --keep-last 20         # dry run: show what would go
    python scripts/prune_runs.py --keep-last 20 --apply  # actually delete
    python scripts/prune_runs.py --keep-days 30 --apply

At least one of --keep-last / --keep-days is required. Runs are kept if
they satisfy EITHER condition (i.e. the two are OR'd — a run is deleted
only if it's both older than --keep-days AND outside the --keep-last N).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"


def _run_started_at(run_dir: pathlib.Path) -> float:
    meta = run_dir / "run.json"
    if meta.is_file():
        try:
            return json.loads(meta.read_text()).get("started_at", 0.0)
        except Exception:
            pass
    return run_dir.stat().st_mtime


def select_prune_candidates(keep_last: int | None, keep_days: float | None) -> list[pathlib.Path]:
    if not RUNS_DIR.is_dir():
        return []
    runs = sorted(
        (d for d in RUNS_DIR.iterdir() if d.is_dir()),
        key=_run_started_at,
        reverse=True,  # newest first
    )
    keep_by_count = set(runs[:keep_last]) if keep_last is not None else set()
    if keep_days is not None:
        cutoff = time.time() - keep_days * 86400
        keep_by_age = {d for d in runs if _run_started_at(d) >= cutoff}
    else:
        keep_by_age = set()
    keep = keep_by_count | keep_by_age
    return [d for d in runs if d not in keep]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-last", type=int, help="always keep the N most recent runs")
    parser.add_argument("--keep-days", type=float, help="always keep runs started within the last N days")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run, deletes nothing)")
    args = parser.parse_args()

    if args.keep_last is None and args.keep_days is None:
        parser.error("specify at least one of --keep-last / --keep-days")

    candidates = select_prune_candidates(args.keep_last, args.keep_days)
    if not candidates:
        print("Nothing to prune.")
        return 0

    verb = "Deleting" if args.apply else "Would delete (dry run — pass --apply to actually delete)"
    print(f"{verb} {len(candidates)} run(s):")
    for d in candidates:
        print(f"  {d.relative_to(ROOT)}")
        if args.apply:
            shutil.rmtree(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
