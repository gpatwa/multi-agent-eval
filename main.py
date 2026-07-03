#!/usr/bin/env python3
"""Multi-provider model evaluation CLI.

Usage:
    python main.py --config config.demo.yaml            # offline mock demo
    python main.py --config config.yaml                 # real providers
    python main.py --config config.yaml --tasks tasks.yaml --out results/
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

from eval_agents.agents import Agent
from eval_agents.judge import JUDGE_SYSTEM
from eval_agents.registry import MissingCredentials, create_provider
from eval_agents.report import to_json, to_markdown
from eval_agents.runner import Task, run_evaluation


def load_agents(config: dict) -> tuple[list[Agent], Agent]:
    candidates: list[Agent] = []
    for spec in config["candidates"]:
        try:
            provider = create_provider(spec["provider"], spec["model"])
        except MissingCredentials as exc:
            print(f"skipping candidate {spec['name']!r}: {exc}", file=sys.stderr)
            continue
        candidates.append(Agent(name=spec["name"], provider=provider))

    if not candidates:
        sys.exit("No candidates available — set at least one provider API key.")

    judge_spec = config["judge"]
    judge = Agent(
        name="judge",
        provider=create_provider(judge_spec["provider"], judge_spec["model"]),
        system=JUDGE_SYSTEM,
    )
    return candidates, judge


def load_tasks(path: str) -> list[Task]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    return [Task(**t) for t in data["tasks"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate LLMs across providers")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tasks", default="tasks.yaml")
    parser.add_argument("--out", default="results")
    args = parser.parse_args()

    config = yaml.safe_load(pathlib.Path(args.config).read_text())
    candidates, judge = load_agents(config)
    tasks = load_tasks(args.tasks)

    print(
        f"Evaluating {len(candidates)} candidates on {len(tasks)} tasks "
        f"(judge: {judge.provider!r})",
        file=sys.stderr,
    )
    results = run_evaluation(tasks, candidates, judge)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(to_json(results))
    report_path = out / "report.md"
    report_path.write_text(to_markdown(results))
    print(f"\nReport written to {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
