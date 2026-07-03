"""Judge agent — LLM-as-judge scoring with a fixed rubric.

The judge is just another Agent, so it can run on any provider. Scoring is
prompt-based JSON (rather than vendor-specific structured-output APIs) so
the same judge logic works identically across all four platforms.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .agents import Agent

JUDGE_SYSTEM = (
    "You are a strict, impartial evaluator of AI model outputs. "
    "You never reward verbosity, style, or confidence — only whether the "
    "answer satisfies the task. Respond with JSON only, no prose, no code fences."
)

JUDGE_PROMPT = """Evaluate the candidate answer against the task.

<task>
{task}
</task>

<reference_notes>
{reference}
</reference_notes>

<candidate_answer>
{answer}
</candidate_answer>

Score each dimension from 1 (poor) to 5 (excellent):
- accuracy: is the content factually/logically correct?
- completeness: does it cover everything the task asked for?
- clarity: is it well-organized and easy to follow?
- instruction_following: does it respect format, length, and constraints?

Respond with exactly this JSON shape and nothing else:
{{"scores": {{"accuracy": n, "completeness": n, "clarity": n, "instruction_following": n}}, "overall": n.n, "rationale": "one or two sentences"}}
"""

DIMENSIONS = ("accuracy", "completeness", "clarity", "instruction_following")


@dataclass
class Verdict:
    scores: dict[str, int] = field(default_factory=dict)
    overall: float = 0.0
    rationale: str = ""
    parse_error: str | None = None


def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip("` \n")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in judge output: {text[:200]!r}")
    return json.loads(match.group(0))


def score(judge: Agent, task_prompt: str, reference: str, answer: str) -> Verdict:
    prompt = JUDGE_PROMPT.format(
        task=task_prompt, reference=reference or "(none provided)", answer=answer
    )
    resp = judge.run(prompt, max_tokens=1024)
    try:
        data = _extract_json(resp.text)
        scores = {d: int(data["scores"][d]) for d in DIMENSIONS}
        overall = float(data.get("overall") or sum(scores.values()) / len(scores))
        return Verdict(scores=scores, overall=overall, rationale=str(data.get("rationale", "")))
    except Exception as exc:  # malformed judge output shouldn't kill the run
        return Verdict(parse_error=f"{type(exc).__name__}: {exc}")
