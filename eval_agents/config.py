"""Shared config/task loading used by both the CLI and the web server."""
from __future__ import annotations

import pathlib
import sys

import yaml
from dotenv import load_dotenv

from .agents import WORKER_SYSTEM, Agent
from .judge import JUDGE_SYSTEM, generic_scorer
from .registry import MissingCredentials, create_provider
from .runner import Executor, Scorer, Task
from .usecases import EXECUTORS, automationbench
from .usecases import REGISTRY as USE_CASES

# Load provider API keys from .env regardless of the caller's cwd or how the
# process was launched (both main.py and webapp/server.py import this module
# before constructing any provider) — previously every run needed a manual
# `source .env` first, which the web UI's dev-server launcher couldn't do.
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")


def select_use_case(config: dict) -> tuple[str, Scorer]:
    """Return (candidate_system_prompt, scorer) for the config's use case.

    Defaults to the generic assistant + LLM-as-judge scorer when no
    `use_case` is set, so existing configs keep working unchanged.
    """
    use_case = config.get("use_case")
    if use_case is None:
        return WORKER_SYSTEM, generic_scorer
    try:
        system, scorer = USE_CASES[use_case]
    except KeyError:
        raise RuntimeError(
            f"Unknown use_case {use_case!r}. Available: {sorted(USE_CASES)}"
        ) from None
    return system, scorer


def select_executor(config: dict) -> Executor | None:
    """The use case's candidate executor (a tool loop), or None for the
    default single completion."""
    return EXECUTORS.get(config.get("use_case"))


def load_agents(config: dict) -> tuple[list[Agent], Agent]:
    """Build candidate and judge agents from a parsed config dict.

    Candidates whose credentials are missing are skipped with a warning;
    raises RuntimeError if none remain.
    """
    candidate_system, _ = select_use_case(config)
    needs_tools = select_executor(config) is not None

    candidates: list[Agent] = []
    for spec in config["candidates"]:
        try:
            provider = create_provider(spec["provider"], spec["model"])
        except MissingCredentials as exc:
            print(f"skipping candidate {spec['name']!r}: {exc}", file=sys.stderr)
            continue
        if needs_tools and not provider.supports_tools:
            print(
                f"skipping candidate {spec['name']!r}: provider {spec['provider']!r} has no tool-use "
                "support (the vendor CLIs run their own agent loop)",
                file=sys.stderr,
            )
            continue
        candidates.append(Agent(name=spec["name"], provider=provider, system=candidate_system))

    if not candidates:
        raise RuntimeError(
            "No candidates available — set at least one provider API key, or "
            "install and log in to a vendor CLI (claude / codex / gemini) for "
            "the subscription providers."
        )

    judge_spec = config["judge"]
    try:
        judge_provider = create_provider(judge_spec["provider"], judge_spec["model"])
    except MissingCredentials as exc:
        raise RuntimeError(
            f"Judge unavailable ({exc}) — the judge is required; set its key or "
            "pick a different judge provider in the config."
        ) from None
    judge = Agent(name="judge", provider=judge_provider, system=JUDGE_SYSTEM)
    return candidates, judge


def load_config(path: str | pathlib.Path) -> dict:
    return yaml.safe_load(pathlib.Path(path).read_text())


def load_tasks(path: str | pathlib.Path) -> list[Task]:
    # `automationbench:<domain>[:<n>]` loads tasks from the pinned
    # AutomationBench package instead of a YAML file. Callers may have joined
    # it onto a directory (the web UI does ROOT / tasks_file), so match the
    # last path component.
    spec = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    if spec.startswith(automationbench.TASK_PREFIX):
        return automationbench.load_tasks(spec)
    data = yaml.safe_load(pathlib.Path(path).read_text())
    return [Task(**t) for t in data["tasks"]]
