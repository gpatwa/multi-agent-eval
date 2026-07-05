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

from eval_agents.config import load_agents, load_config, load_tasks, select_use_case
from eval_agents.report import to_json, to_markdown
from eval_agents.runner import run_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLMs across providers")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tasks", default=None, help="task file (default: config's `tasks:` or tasks.yaml)")
    parser.add_argument("--out", default="results")
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
    results = run_evaluation(tasks, candidates, judge, scorer=scorer)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(to_json(results))
    report_path = out / "report.md"
    report_path.write_text(to_markdown(results, scorecard=config.get("scorecard")))
    print(f"\nReport written to {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
