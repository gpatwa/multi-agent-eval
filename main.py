#!/usr/bin/env python3
"""Multi-provider model evaluation CLI.

Usage:
    python main.py --config config.demo.yaml            # offline mock demo
    python main.py --config config.yaml                 # real providers
    python main.py --config config.yaml --tasks tasks.yaml --out results/

For the web UI, run:  uvicorn webapp.server:app --reload
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import json

from eval_agents.config import load_agents, load_config, load_tasks, select_use_case
from eval_agents.report import summarize, to_json, to_markdown, to_summary_json
from eval_agents.runner import run_evaluation


def check_regression(summary: dict, baseline_dir: str, threshold: float) -> list[str]:
    """Compare this run against a previous run's summary.json.

    A regression is: quality drop > threshold, or any increase in critical
    violations, for a candidate present in both runs.
    """
    baseline_path = pathlib.Path(baseline_dir) / "summary.json"
    baseline = json.loads(baseline_path.read_text())
    problems = []
    for name, now in summary["candidates"].items():
        then = baseline.get("candidates", {}).get(name)
        if not then:
            continue
        dq = now["quality_mean"] - then["quality_mean"]
        if dq < -threshold:
            problems.append(
                f"{name}: quality {then['quality_mean']:.2f} -> {now['quality_mean']:.2f} ({dq:+.2f})"
            )
        dv = now["critical_violations"] - then.get("critical_violations", 0)
        if dv > 0:
            problems.append(f"{name}: critical violations {then.get('critical_violations', 0)} -> {now['critical_violations']}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLMs across providers")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tasks", default=None, help="task file (default: config's `tasks:` or tasks.yaml)")
    parser.add_argument("--out", default="results")
    parser.add_argument("--trials", type=int, default=1, help="repeat each task N times to measure variance")
    parser.add_argument("--baseline", default=None, help="previous --out dir; exit 1 if quality regresses vs it")
    parser.add_argument("--regression-threshold", type=float, default=0.3,
                        help="max allowed quality-mean drop vs baseline (default 0.3)")
    args = parser.parse_args()

    config = load_config(args.config)
    try:
        candidates, judge = load_agents(config)
        _, scorer = select_use_case(config)
    except RuntimeError as exc:
        sys.exit(str(exc))
    tasks_file = args.tasks or config.get("tasks", "tasks.yaml")
    tasks = load_tasks(tasks_file)

    print(
        f"Evaluating {len(candidates)} candidates on {len(tasks)} tasks "
        f"(judge: {judge.provider!r})",
        file=sys.stderr,
    )
    results = run_evaluation(tasks, candidates, judge, scorer=scorer, trials=args.trials)

    scorecard = config.get("scorecard")
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(to_json(results))
    (out / "summary.json").write_text(to_summary_json(results, scorecard))
    report_path = out / "report.md"
    report_path.write_text(to_markdown(results, scorecard=scorecard))
    print(f"\nReport written to {report_path}", file=sys.stderr)

    if args.baseline:
        problems = check_regression(summarize(results, scorecard), args.baseline, args.regression_threshold)
        if problems:
            print(f"\nREGRESSION vs baseline {args.baseline}:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            sys.exit(1)
        print(f"No regression vs baseline {args.baseline}.", file=sys.stderr)


if __name__ == "__main__":
    main()
