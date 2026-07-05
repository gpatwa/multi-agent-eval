"""Orchestrator — fans each task out to all candidate agents in parallel,
then has the judge score every answer.

Flow per task:
    task ──▶ [candidate agents, concurrently] ──▶ answers ──▶ judge ──▶ verdicts
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .agents import Agent
from .judge import Verdict, generic_scorer

# A scorer turns (judge, task, candidate_answer) into a Verdict. The generic
# LLM-as-judge scorer is the default; use cases (e.g. triage) supply their own.
Scorer = Callable[[Agent, "Task", str], Verdict]


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    reference: str = ""
    gold: dict = field(default_factory=dict)  # use-case ground truth (e.g. category/priority)


@dataclass
class CandidateResult:
    candidate: str
    model: str
    trial: int = 0  # 0-based trial index (repeat runs of the same task)
    answer: str = ""
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    verdict: Verdict | None = None


@dataclass
class TaskResult:
    task: Task
    results: list[CandidateResult] = field(default_factory=list)


def _run_candidate(agent: Agent, task: Task) -> CandidateResult:
    try:
        resp = agent.run(task.prompt)
        return CandidateResult(
            candidate=agent.name,
            model=resp.model,
            answer=resp.text,
            latency_s=resp.latency_s,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        )
    except Exception as exc:
        return CandidateResult(
            candidate=agent.name,
            model=agent.provider.model,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_evaluation(
    tasks: list[Task],
    candidates: list[Agent],
    judge: Agent,
    scorer: Scorer | None = None,
    trials: int = 1,  # repeat each task N times per candidate to measure variance
    on_task_done=None,  # callback(task_result, done_count, total) for live progress
) -> list[TaskResult]:
    scorer = scorer or generic_scorer
    trials = max(1, trials)
    all_results: list[TaskResult] = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task.id} ({task.category})", file=sys.stderr)

        results: list[CandidateResult] = []
        for trial in range(trials):
            with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
                batch = list(pool.map(lambda a: _run_candidate(a, task), candidates))
            for r in batch:
                r.trial = trial
            results.extend(batch)

        for result in results:
            marker = f" t{result.trial + 1}" if trials > 1 else ""
            if result.error:
                print(f"    {result.candidate}{marker}: ERROR {result.error}", file=sys.stderr)
                continue
            result.verdict = scorer(judge, task, result.answer)
            shown = (
                f"overall {result.verdict.overall}"
                if not result.verdict.parse_error
                else f"judge parse error: {result.verdict.parse_error}"
            )
            print(f"    {result.candidate}{marker}: {shown} ({result.latency_s:.1f}s)", file=sys.stderr)

        task_result = TaskResult(task=task, results=results)
        all_results.append(task_result)
        if on_task_done:
            on_task_done(task_result, i, len(tasks))
    return all_results
