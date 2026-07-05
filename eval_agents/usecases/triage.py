"""Support ticket triage/reply — a concrete customer-problem benchmark.

The customer problem: an incoming support ticket must be (1) routed to the
right queue, (2) assigned a priority, and (3) answered with a policy-compliant
reply. Picking the model to ship for this is a real decision, so scoring is
decision-grade rather than a single vibe score:

  * routing + priority are graded DETERMINISTICALLY against gold labels
    (a mis-route is objectively wrong — the judge doesn't get a vote), and
  * the reply is graded by the LLM judge against company policy for
    policy-adherence, resolution quality, and tone.

A candidate that fails to emit valid JSON scores low (it broke the contract),
rather than being excluded — that failure mode matters for this use case.
"""
from __future__ import annotations

import json
import re

from ..agents import Agent
from ..judge import Verdict, _extract_json

# ---------------------------------------------------------------- taxonomy
CATEGORIES = ["billing", "technical", "account_access", "feature_request", "cancellation"]
PRIORITIES = ["urgent", "high", "normal", "low"]

# The support policy both the candidate (as its operating rules) and the judge
# (as the grading standard) are given. Keeping it in one place means the agent
# and the grader can never drift apart.
POLICY = """\
Northwind Cloud — Support Triage Policy

Categories (route to exactly one): billing, technical, account_access,
feature_request, cancellation.

Priority rules:
- urgent: full outage / data loss / suspected security breach or account takeover.
- high: login blocked or password reset failing; a double/duplicate charge;
  a charge after a confirmed cancellation.
- normal: how-to and configuration questions; single billing discrepancies;
  refund requests; cancellations.
- low: feature requests and cosmetic suggestions.

Reply rules:
- Refunds: FULL refund only within 14 days of the charge. After 14 days, annual
  plans get a PRORATED refund for unused time; monthly plans get NO refund.
  Never promise a refund the policy does not allow.
- Cancellations: offer to pause the plan (retention) ONCE, then honor the cancel.
- Feature requests: thank them and log it; NEVER promise a timeline or that it
  will be built.
- Security/account-takeover: treat as urgent, advise an immediate password reset,
  and say the security team is engaged.
- Always be empathetic and professional, never blame the customer, and end with a
  concrete next step or ETA. Never reference another customer's data.
"""

TRIAGE_SYSTEM = f"""You are a support triage assistant for Northwind Cloud.
For each customer ticket, decide the category and priority and write the reply
the customer will receive, following the policy exactly.

{POLICY}

Respond with ONLY a JSON object, no prose and no code fences:
{{"category": "<one of: {', '.join(CATEGORIES)}>", "priority": "<one of: {', '.join(PRIORITIES)}>", "reply": "<the customer-facing reply>"}}"""

# Scoring weights within the triage overall (on the shared 1-5 scale).
_WEIGHTS = {
    "routing": 0.30,
    "priority": 0.20,
    "policy_adherence": 0.25,
    "resolution": 0.15,
    "tone": 0.10,
}
DIMENSIONS = tuple(_WEIGHTS)  # display order

_JUDGE_PROMPT = """You are grading only the REPLY a support agent sent for a ticket.
Route/priority are graded separately — judge the reply text only.

<policy>
{policy}
</policy>

<ticket>
{ticket}
</ticket>

<ideal_handling>
{reference}
</ideal_handling>

<agent_reply>
{reply}
</agent_reply>

Score each from 1 (poor) to 5 (excellent):
- policy_adherence: does the reply obey the policy? Promising a refund/timeline the
  policy forbids is an automatic 1.
- resolution: does it actually resolve the issue or give a correct, concrete next step?
- tone: empathetic, professional, non-blaming, appropriate to the customer's mood?

Respond with ONLY this JSON:
{{"scores": {{"policy_adherence": n, "resolution": n, "tone": n}}, "rationale": "one sentence"}}"""


def _norm(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def parse_response(text: str) -> dict | None:
    """Pull {category, priority, reply} from a candidate's JSON output."""
    try:
        data = _extract_json(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "category": str(data.get("category", "")),
        "priority": str(data.get("priority", "")),
        "reply": str(data.get("reply", "")),
    }


def triage_scorer(judge: Agent, task, answer: str) -> Verdict:
    gold = task.gold or {}
    parsed = parse_response(answer)

    if parsed is None:
        # Broke the output contract → real triage failure, scored (not excluded).
        scores = {d: 1 for d in DIMENSIONS}
        return Verdict(scores=scores, overall=1.0, rationale="Response was not valid JSON.")

    routing = 5 if _norm(parsed["category"]) == _norm(gold.get("category", "")) else 1
    priority = 5 if _norm(parsed["priority"]) == _norm(gold.get("priority", "")) else 1

    # Judge grades the reply text against policy.
    prompt = _JUDGE_PROMPT.format(
        policy=POLICY,
        ticket=task.prompt,
        reference=task.reference or "(none)",
        reply=parsed["reply"],
    )
    resp = judge.run(prompt, max_tokens=512)
    try:
        data = _extract_json(resp.text)
        reply_scores = {k: int(data["scores"][k]) for k in ("policy_adherence", "resolution", "tone")}
        rationale = str(data.get("rationale", ""))
    except Exception as exc:
        # Judge failure is different from candidate failure — surface it, but
        # still report the objective routing/priority we already know.
        scores = {"routing": routing, "priority": priority, "policy_adherence": 0, "resolution": 0, "tone": 0}
        return Verdict(scores=scores, parse_error=f"judge: {type(exc).__name__}: {exc}")

    scores = {"routing": routing, "priority": priority, **reply_scores}
    overall = round(sum(_WEIGHTS[d] * scores[d] for d in DIMENSIONS), 2)
    got = f"routed {parsed['category']}/{parsed['priority']}"
    want = f"gold {gold.get('category')}/{gold.get('priority')}"
    note = f"{got} vs {want}. {rationale}"
    return Verdict(scores=scores, overall=overall, rationale=note)
