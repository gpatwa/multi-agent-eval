"""AutomationBench lane — Zapier's public agentic benchmark inside this harness.

Runs AutomationBench tasks (https://github.com/zapier/AutomationBench, MIT,
(c) 2026 Zapier, Inc.) through this repo's providers and tool loop, so they
land in the same scorecard as your own policy suite: latency, cost, CIs,
regression gating. AutomationBench itself supplies everything that defines
the task — simulated app state (WorldState), the per-task tool subset
(their "limited_zapier" toolset), the system prompt, and the assertion
scorer (`partial_credit`, used unmodified) — so the pass/fail semantics are
theirs, not a re-implementation.

Differences from their own runner, so numbers are directional rather than
leaderboard-comparable:
  * this is their PUBLIC task set; the official leaderboard uses a harder
    held-out set,
  * our loop and provider adapters drive the model (default request
    settings; no per-vendor reasoning-effort flag), and
  * assertion handler errors count as failed assertions instead of
    crashing the run (AUTOMATIONBENCH_STRICT_ASSERTIONS=0).

Install the optional extra first (Python >= 3.13):
    pip install -r requirements-automationbench.txt

Tasks are referenced as `automationbench:<domain>[:<n>]` in a config's
`tasks:` (or --tasks); `:<n>` takes a fixed-seed sample of n tasks.
"""
from __future__ import annotations

import copy
import functools
import inspect
import json
import os
import random
import time

from ..agents import Agent
from ..judge import Verdict
from ..providers.base import ChatMessage, ModelResponse, ToolResult, ToolSpec

TASK_PREFIX = "automationbench:"
DOMAINS = ("support",)  # wired so far; the other five domains load the same way
MAX_STEPS = 50  # AutomationBench's own default --max-steps
SAMPLE_SEED = 20260925  # fixed so a ":<n>" sample is the same tasks every run
_MAX_FAILED_SHOWN = 8  # failed assertions kept in the answer record

# Placeholder for the use-case registry: the real system prompt is
# AutomationBench's own, taken from each task row by run_candidate.
AB_SYSTEM = "(AutomationBench task system prompt — supplied per task)"


def _require():
    # Treat buggy assertion handlers as failed assertions, not a crashed run.
    # Must be set before AutomationBench's rubric registry is imported.
    os.environ.setdefault("AUTOMATIONBENCH_STRICT_ASSERTIONS", "0")
    try:
        import automationbench  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "use_case 'automationbench' needs the optional AutomationBench package "
            "(Python >= 3.13): pip install -r requirements-automationbench.txt"
        ) from exc


@functools.lru_cache(maxsize=None)
def _dataset(domain: str) -> dict[int, dict]:
    """example_id -> {system, user, info} for one domain (built once)."""
    _require()
    if domain not in DOMAINS:
        raise RuntimeError(f"AutomationBench domain {domain!r} not wired; available: {DOMAINS}")
    from automationbench.domains import get_domain_dataset

    rows = {}
    for row in get_domain_dataset(domain):
        info = json.loads(row["info"]) if isinstance(row["info"], str) else row["info"]
        rows[row["example_id"]] = {
            "system": row["prompt"][0]["content"],
            "user": row["prompt"][-1]["content"],
            "info": info,
        }
    return rows


@functools.lru_cache(maxsize=None)
def _tooling():
    """(tool name -> ToolSpec, tool name -> function) from AutomationBench's registry.

    Mirrors what their env's add_tool() does (convert the signature, then
    hide the injected `world` argument) without constructing the env, which
    would install a SIGINT handler (breaking our Ctrl-C partial-results path)
    and can only run on the main thread."""
    _require()
    from automationbench.tools import ALL_TOOLS
    from verifiers.legacy.envs.stateful_tool_env import filter_signature
    from verifiers.legacy.utils.tool_utils import convert_func_to_tool_def

    specs, fns = {}, {}
    for fn in ALL_TOOLS:
        skip = ["world"] if "world" in inspect.signature(fn).parameters else []
        tool_def = convert_func_to_tool_def(filter_signature(fn, skip))
        params = copy.deepcopy(tool_def.parameters)
        for arg in skip:
            prop = params.get("properties", {}).pop(arg, None) or {}
            if "$ref" in prop:
                params.get("$defs", {}).pop(prop["$ref"].split("/")[-1], None)
            if arg in params.get("required", []):
                params["required"].remove(arg)
        specs[tool_def.name] = ToolSpec(tool_def.name, tool_def.description, params)
        fns[fn.__name__] = fn
    return specs, fns


def load_tasks(spec: str) -> list:
    """Build Task objects for `automationbench:<domain>[:<n>]`."""
    from ..runner import Task

    parts = spec[len(TASK_PREFIX):].split(":")
    domain, n = parts[0], (int(parts[1]) if len(parts) > 1 and parts[1] else None)
    rows = _dataset(domain)
    ids = sorted(rows)
    if n is not None and n < len(ids):
        ids = sorted(random.Random(SAMPLE_SEED).sample(ids, n))
    return [
        Task(
            id=rows[i]["info"]["task_name"],
            category=f"automationbench/{domain}",
            prompt=rows[i]["user"],
            # Only a pointer: the full initial state is large and is rebuilt
            # from the pinned dataset at run time, keeping results.json small.
            fixture={"automationbench": {"domain": domain, "example_id": i}},
        )
        for i in ids
    ]


class ABSandbox:
    """One task's AutomationBench world plus its allowed tools."""

    def __init__(self, info: dict):
        from automationbench.runner import compute_allowed_services, strip_none_values
        from automationbench.schema.world import WorldState

        self.info = copy.deepcopy(info)
        initial = strip_none_values(self.info["initial_state"])
        self.info["assertions"] = [strip_none_values(a) for a in self.info["assertions"]]
        self.world = WorldState(**initial)
        self.world.meta.allowed_services = compute_allowed_services(
            initial, self.info["assertions"], self.info["zapier_tools"]
        )
        self.initial = copy.deepcopy(initial)
        specs, self._fns = _tooling()
        self.tools = [specs[name] for name in self.info["zapier_tools"]]
        self._allowed = set(self.info["zapier_tools"])
        self.calls = self.errors = 0

    def execute(self, call) -> ToolResult:
        self.calls += 1
        if call.name not in self._allowed:
            self.errors += 1
            return ToolResult(call.id, call.name, f"error: tool {call.name!r} is not available", is_error=True)
        # Same normalization as their runner: {} means "argument omitted".
        args = {k: v for k, v in call.arguments.items() if not (isinstance(v, dict) and not v)}
        try:
            out = self._fns[call.name](world=self.world, **args)
        except Exception as exc:  # tool raised on bad input — report it back to the model
            self.errors += 1
            return ToolResult(call.id, call.name, f"error: {type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(call.id, call.name, out if isinstance(out, str) else json.dumps(out, default=str))

    def score(self) -> dict:
        from automationbench.rubric import partial_credit

        state = {"info": self.info, "world": self.world, "initial_state": self.initial}
        credit = partial_credit(state)
        results = [r for r in state.get("_assertion_results", []) if not r["excluded"]]
        failed = [{"type": r["type"], "params": r["params"]} for r in results if not r["passed"]]
        return {
            "partial_credit": round(credit, 4),
            "completed": credit == 1.0,  # AutomationBench's task_completed_correctly
            "assertions_passed": len(results) - len(failed),
            "assertions_scored": len(results),
            "failed": failed[:_MAX_FAILED_SHOWN],
        }


def run_candidate(agent: Agent, task) -> ModelResponse:
    """Executor: drive the candidate's tool loop in the task's AutomationBench world."""
    ref = task.fixture["automationbench"]
    row = _dataset(ref["domain"])[ref["example_id"]]
    box = ABSandbox(row["info"])
    history: list = [ChatMessage(role="user", content=row["user"])]
    tokens_in = tokens_out = 0
    model, cut_off = agent.provider.model, True
    start = time.perf_counter()
    for _ in range(MAX_STEPS):
        turn = agent.provider.complete_with_tools(history, box.tools, system=row["system"], max_tokens=8192)
        tokens_in += turn.input_tokens
        tokens_out += turn.output_tokens
        model = turn.model
        history.append(turn)
        if not turn.tool_calls:
            cut_off = False
            break
        history.append([box.execute(c) for c in turn.tool_calls])
    latency = time.perf_counter() - start
    record = {**box.score(), "tool_calls": box.calls, "tool_errors": box.errors, "cut_off": cut_off}
    return ModelResponse(
        text=json.dumps(record), model=model,
        input_tokens=tokens_in, output_tokens=tokens_out, latency_s=latency,
    )


def automationbench_scorer(judge: Agent, task, answer: str) -> Verdict:
    """Deterministic: AutomationBench's partial credit on the 1-5 scale, plus
    its strict pass/fail. The judge is not called."""
    try:
        record = json.loads(answer)
        credit = float(record["partial_credit"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return Verdict(scores={"assertions": 1}, overall=1.0, passed=False,
                       rationale="Run produced no AutomationBench score.")
    shown = "; ".join(f["type"] for f in record.get("failed", []))
    note = (
        f"{record['assertions_passed']}/{record['assertions_scored']} assertions"
        + (f"; failed: {shown}" if shown else "")
        + ("; hit step limit" if record.get("cut_off") else "")
    )
    return Verdict(
        scores={"assertions": round(1 + 4 * credit, 2)},
        overall=round(1 + 4 * credit, 2),
        passed=bool(record["completed"]),
        rationale=note,
    )
