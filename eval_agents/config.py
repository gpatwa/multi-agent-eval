"""Shared config/task loading used by both the CLI and the web server."""
from __future__ import annotations

import pathlib
import sys

import yaml

from .agents import Agent
from .judge import JUDGE_SYSTEM
from .registry import MissingCredentials, create_provider
from .runner import Task


def load_agents(config: dict) -> tuple[list[Agent], Agent]:
    """Build candidate and judge agents from a parsed config dict.

    Candidates whose credentials are missing are skipped with a warning;
    raises RuntimeError if none remain.
    """
    candidates: list[Agent] = []
    for spec in config["candidates"]:
        try:
            provider = create_provider(spec["provider"], spec["model"])
        except MissingCredentials as exc:
            print(f"skipping candidate {spec['name']!r}: {exc}", file=sys.stderr)
            continue
        candidates.append(Agent(name=spec["name"], provider=provider))

    if not candidates:
        raise RuntimeError("No candidates available — set at least one provider API key.")

    judge_spec = config["judge"]
    judge = Agent(
        name="judge",
        provider=create_provider(judge_spec["provider"], judge_spec["model"]),
        system=JUDGE_SYSTEM,
    )
    return candidates, judge


def load_config(path: str | pathlib.Path) -> dict:
    return yaml.safe_load(pathlib.Path(path).read_text())


def load_tasks(path: str | pathlib.Path) -> list[Task]:
    data = yaml.safe_load(pathlib.Path(path).read_text())
    return [Task(**t) for t in data["tasks"]]
