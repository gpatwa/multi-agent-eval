"""Combine candidates from separate runs of the same tasks into one scorecard.

    python scripts/merge_runs.py results-triage-run5-low-claude-gemini results-triage-run5-low-gpt \
        --config config.triage.mixed.yaml --out results-triage-run5-low

When a candidate can't run alongside the others (its subscription hit a usage
limit, its API key arrived later), run it separately and merge. The composite
score min-max normalizes latency and cost ACROSS candidates, so per-run
summaries can't just be stitched together: this re-summarizes the combined
results from scratch.

Guards, because a merged scorecard implies the candidates were compared fairly:
  * every run must cover the same tasks in the same order,
  * candidate names must be unique across runs,
  * every run must have been judged by the same judge with the same settings
    (pass --allow-judge-mismatch to override, which is recorded in the report),
  * the run settings (model, effort, CLI versions) of every run are kept, and
    the report says whether effort is matched across the union.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.config import load_config  # noqa: E402
from eval_agents.report import to_json, to_markdown, to_summary_json  # noqa: E402
from eval_agents.results_io import load_results, load_settings  # noqa: E402
from eval_agents.runner import TaskResult  # noqa: E402


class MergeError(RuntimeError):
    pass


def merge_results(runs: list[list[TaskResult]], names: list[str]) -> list[TaskResult]:
    """Combine per-run results task by task (tasks must match exactly)."""
    base = runs[0]
    for name, run in zip(names[1:], runs[1:]):
        if [tr.task.id for tr in run] != [tr.task.id for tr in base]:
            raise MergeError(f"{name} covers different tasks (or order) than {names[0]}: merging would "
                             "compare candidates on different questions")
    seen: dict[str, str] = {}
    merged = [TaskResult(task=tr.task, results=[]) for tr in base]
    for name, run in zip(names, runs):
        for out, tr in zip(merged, run):
            for r in tr.results:
                owner = seen.setdefault(r.candidate, name)
                if owner != name:
                    raise MergeError(f"candidate {r.candidate!r} appears in both {owner} and {name}")
                out.results.append(r)
    return merged


def merge_settings(all_settings: list[dict | None], names: list[str], allow_judge_mismatch: bool) -> dict:
    """Union of candidate settings; refuses mixed judges unless overridden."""
    missing = [n for n, s in zip(names, all_settings) if not s]
    if missing:
        raise MergeError(f"no run_settings in {', '.join(missing)} (produced before settings were recorded); "
                         "can't verify how those candidates were run")
    # The judge's identity is provider + model + effort. A CLI patch release between
    # runs (cli_version) doesn't change which model judged, so it's noted, not refused.
    identity = lambda j: (j["provider"], j["model"], j["effort"])  # noqa: E731
    judges = [identity(s["judge"]) for s in all_settings]
    notes = []
    versions = {s["judge"].get("cli_version") for s in all_settings} - {None}
    if len(versions) > 1:
        notes.append(f"judge CLI version differs across merged runs ({', '.join(sorted(versions))})")
    if len(set(judges)) > 1:
        detail = "; ".join(f"{n}: {s['judge']['model']} effort {s['judge']['effort']}" for n, s in zip(names, all_settings))
        if not allow_judge_mismatch:
            raise MergeError(f"runs were judged differently ({detail}); merged verdicts wouldn't be comparable. "
                             "Re-judge with scripts/rejudge.py or pass --allow-judge-mismatch")
        notes.append(f"judges differ across merged runs ({detail})")
    candidates = {}
    for s in all_settings:
        candidates.update(s["candidates"])
    efforts = {n: c["effort"] for n, c in candidates.items()}
    matched = len(set(efforts.values())) == 1 and next(iter(efforts.values())) != "default"
    return {
        "effort_matched": matched, "effort_by_candidate": efforts, "candidates": candidates,
        "judge": all_settings[0]["judge"], "merged_from": names, **({"notes": notes} if notes else {}),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", type=pathlib.Path, help="run directories to combine (2 or more)")
    ap.add_argument("--config", required=True, help="config whose scorecard (weights, pricing) to apply")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--allow-judge-mismatch", action="store_true")
    args = ap.parse_args(argv)
    if len(args.runs) < 2:
        ap.error("need at least two runs to merge")

    names = [str(r) for r in args.runs]
    try:
        merged = merge_results([load_results(r, with_verdicts=True) for r in args.runs], names)
        settings = merge_settings([load_settings(r) for r in args.runs], names, args.allow_judge_mismatch)
    except MergeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    scorecard = load_config(args.config).get("scorecard")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(to_json(merged))
    (args.out / "summary.json").write_text(to_summary_json(merged, scorecard, settings=settings))
    (args.out / "report.md").write_text(to_markdown(merged, scorecard=scorecard, settings=settings))
    (args.out / "merged_from.txt").write_text("\n".join(names) + "\n")
    print(f"Merged {len(names)} runs into {args.out / 'report.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
