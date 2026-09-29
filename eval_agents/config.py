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


def select_trials(config: dict, override: int | None = None) -> int:
    """Trials per task: an explicit override (e.g. --trials) wins, then the
    config's `trials:`, else 1."""
    value = override if override is not None else config.get("trials", 1)
    try:
        trials = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"`trials` must be a positive integer, got {value!r}") from None
    if trials < 1:
        raise RuntimeError(f"`trials` must be a positive integer, got {trials}")
    return trials


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def select_effort(config: dict, spec: dict, inherit: bool = True) -> str | None:
    """Reasoning effort for one candidate/judge spec: its own `effort:`, else (for
    candidates) the config's top-level `effort:`, else None (vendor default).

    Judges don't inherit the top-level value: it exists so *candidates* are compared
    at matched effort, and changing a judge's effort changes every verdict."""
    value = spec.get("effort", config.get("effort") if inherit else None)
    if value is None:
        return None
    if value not in EFFORT_LEVELS:
        raise RuntimeError(f"`effort` must be one of {list(EFFORT_LEVELS)}, got {value!r}")
    return value


def describe_run(config: dict, candidates: list[Agent], judge: Agent) -> dict:
    """What each candidate and the judge actually ran with — stored next to results
    so a comparison can be audited (model IDs, effort, CLI versions, personal CLI
    settings) instead of trusted."""
    candidate_info = {a.name: a.provider.describe() for a in candidates}
    efforts = {name: info["effort"] for name, info in candidate_info.items()}
    matched = len(set(efforts.values())) == 1 and next(iter(efforts.values())) != "default"
    return {
        "effort_matched": matched,
        "effort_by_candidate": efforts,
        "candidates": candidate_info,
        "judge": judge.provider.describe(),
    }


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
            provider = create_provider(spec["provider"], spec["model"], effort=select_effort(config, spec))
        except MissingCredentials as exc:
            print(f"skipping candidate {spec['name']!r}: {exc}", file=sys.stderr)
            continue
        except ValueError as exc:  # e.g. this adapter can't apply the requested effort
            raise RuntimeError(f"candidate {spec['name']!r}: {exc}") from None
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
        judge_provider = create_provider(
            judge_spec["provider"], judge_spec["model"], effort=select_effort(config, judge_spec, inherit=False)
        )
    except ValueError as exc:
        raise RuntimeError(f"judge: {exc}") from None
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
