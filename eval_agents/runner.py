"""Orchestrator — fans each task out to all candidate agents in parallel,
then has the judge score every answer.

Flow per task:
    task ──▶ [candidate agents, concurrently] ──▶ answers ──▶ judge ──▶ verdicts
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .agents import Agent
from .judge import Verdict, score


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    reference: str = ""


@dataclass
class CandidateResult:
    candidate: str
    model: str
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


def run_evaluation(tasks: list[Task], candidates: list[Agent], judge: Agent) -> list[TaskResult]:
    all_results: list[TaskResult] = []
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task.id} ({task.category})", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            results = list(pool.map(lambda a: _run_candidate(a, task), candidates))

        for result in results:
            if result.error:
                print(f"    {result.candidate}: ERROR {result.error}", file=sys.stderr)
                continue
            result.verdict = score(judge, task.prompt, task.reference, result.answer)
            shown = (
                f"overall {result.verdict.overall}"
                if not result.verdict.parse_error
                else f"judge parse error: {result.verdict.parse_error}"
            )
            print(f"    {result.candidate}: {shown} ({result.latency_s:.1f}s)", file=sys.stderr)

        all_results.append(TaskResult(task=task, results=results))
    return all_results
