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

What this lane adds on top (AutomationBench deliberately grades only
verifiable state — its text checks are substring matches): every
external-facing message the agent sends (a public helpdesk reply to a
customer, or an email to a non-internal address such as a customer, vendor or
partner) is graded by the LLM judge for tone, clarity and appropriateness for
its recipient, and checked deterministically for unfilled template
placeholders and echoed PII. Message quality blends into `overall` at
MESSAGE_WEIGHT; AutomationBench's own pass/fail (`passed`) is never touched.

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
import re
import time

from ..agents import Agent
from ..json_extract import extract_json
from ..judge import Verdict, judge_usage
from ..providers.base import ChatMessage, ModelResponse, ToolResult, ToolSpec

TASK_PREFIX = "automationbench:"
DOMAINS = ("support",)  # wired so far; the other five domains load the same way
MAX_STEPS = 50  # AutomationBench's own default --max-steps
SAMPLE_SEED = 20260925  # fixed so a ":<n>" sample is the same tasks every run
_MAX_FAILED_SHOWN = 8  # failed assertions kept in the answer record
MESSAGE_WEIGHT = 0.25  # share of `overall` from judged message quality (when any were sent)
_MAX_MESSAGES_JUDGED = 8
_MAX_BODY_CHARS = 4000
# Diagnostics kept in the answer record (results.json): enough to see why a
# run failed or stopped early without bloating 100-task result files.
_MAX_TRACE_ENTRIES = 200
_TRACE_ARGS_CHARS = 300
_TRACE_RESULT_CHARS = 200
_FINAL_TEXT_CHARS = 2000
_EARLY_STOP_CALLS = 5  # an unfinished task with this few tool calls gets its final text in the rationale

# AutomationBench's convention for the company's own addresses (staff, team
# aliases): x@company.example.com, x@ourcompany.example.com. Email to anyone
# else (customers, vendors, partners) is external-facing.
INTERNAL_DOMAIN_SUFFIXES = ("company.example.com",)

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


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _addresses(*values) -> list[str]:
    out = []
    for v in values:
        for part in (v if isinstance(v, list) else str(v or "").replace(";", ",").split(",")):
            part = str(part).strip().lower()
            if "@" in part:
                out.append(part.split("<")[-1].rstrip(">"))
    return out


def _is_internal(address: str) -> bool:
    return address.split("@")[-1].endswith(INTERNAL_DOMAIN_SUFFIXES)


def external_message(tool: str, args: dict) -> dict | None:
    """The external-facing message a successful tool call sent, or None.

    Visibility follows each tool's own arguments and defaults: Zendesk and
    Zoho comments default public, Freshdesk notes default private, Re:amaze
    messages are customer-visible unless "internal", Gorgias agent messages
    unless on an internal-note channel, Intercom admin replies, Help Scout
    replies. Slack, internal notes and unsent drafts never are."""
    a = args
    if tool == "gmail_send_email":
        recipients = _addresses(a.get("to"), a.get("cc"))
        external = [r for r in recipients if not _is_internal(r)]
        if not external:
            return None
        return {"channel": "email", "to": ", ".join(external), "subject": a.get("subject") or "", "body": a.get("body")}
    if tool == "zendesk_add_comment_to_ticket" and _bool(a.get("public"), True):
        return {"channel": "zendesk public comment", "to": f"ticket {a.get('ticket_id')}", "body": a.get("comment")}
    if tool == "zoho_desk_add_comment" and _bool(a.get("is_public"), True):
        return {"channel": "zoho desk public comment", "to": f"ticket {a.get('ticket_id')}", "body": a.get("content")}
    if tool == "freshdesk_add_note_to_ticket" and not _bool(a.get("private"), True):
        return {"channel": "freshdesk public note", "to": f"ticket {a.get('ticket_id')}", "body": a.get("body")}
    if tool == "reamaze_add_message" and a.get("visibility", "regular") != "internal" and a.get("author_type", "staff") != "customer":
        return {"channel": "re:amaze message", "to": f"conversation {a.get('conversation_id')}", "body": a.get("body")}
    if tool == "reamaze_create_conversation":
        return {"channel": "re:amaze new conversation", "to": a.get("contact_email") or "", "subject": a.get("subject") or "", "body": a.get("body")}
    if tool == "gorgias_create_ticket_message" and a.get("sender_type", "agent") == "agent" and "internal" not in str(a.get("channel") or "").lower():
        return {"channel": "gorgias message", "to": f"ticket {a.get('ticket_id')}", "body": a.get("body_text") or a.get("body") or a.get("body_html")}
    if tool == "helpscout_send_reply":
        return {"channel": "help scout reply", "to": f"conversation {a.get('conversation_id')}", "body": a.get("body")}
    if tool == "intercom_reply_to_conversation" and a.get("author_type", "admin") == "admin":
        return {"channel": "intercom reply", "to": f"conversation {a.get('conversation_id')}", "body": a.get("body")}
    return None


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
        self.messages: list[dict] = []  # external-facing messages actually sent
        self.trace: list[dict] = []  # compact per-call log for diagnosis
        self.step = 0  # assistant turn the current calls belong to (set by run_candidate)

    def _record(self, call, result: ToolResult) -> ToolResult:
        if len(self.trace) < _MAX_TRACE_ENTRIES:
            self.trace.append({
                "step": self.step,
                "tool": call.name,
                "args": _clip(json.dumps(call.arguments, default=str), _TRACE_ARGS_CHARS),
                "error": result.is_error,
                "result": _clip(result.content, _TRACE_RESULT_CHARS),
            })
        return result

    def execute(self, call) -> ToolResult:
        return self._record(call, self._execute(call))

    def _execute(self, call) -> ToolResult:
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
        msg = external_message(call.name, args)
        if msg and str(msg.get("body") or "").strip():
            msg["body"] = str(msg["body"])[:_MAX_BODY_CHARS]
            self.messages.append(msg)
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
    model, cut_off, final_text, steps = agent.provider.model, True, "", 0
    start = time.perf_counter()
    for steps in range(1, MAX_STEPS + 1):
        turn = agent.provider.complete_with_tools(history, box.tools, system=row["system"], max_tokens=8192)
        tokens_in += turn.input_tokens
        tokens_out += turn.output_tokens
        model = turn.model
        if turn.text.strip():
            final_text = turn.text  # the last thing the model said, tools or not
        history.append(turn)
        if not turn.tool_calls:
            cut_off = False
            break
        box.step = steps
        history.append([box.execute(c) for c in turn.tool_calls])
    latency = time.perf_counter() - start
    record = {
        **box.score(), "tool_calls": box.calls, "tool_errors": box.errors, "cut_off": cut_off,
        "steps": steps,
        "final_text": _clip(final_text, _FINAL_TEXT_CHARS),
        "external_messages": box.messages,
        "trace": box.trace,
        "trace_truncated": box.calls > len(box.trace),
    }
    return ModelResponse(
        text=json.dumps(record), model=model,
        input_tokens=tokens_in, output_tokens=tokens_out, latency_s=latency,
    )


_MESSAGE_JUDGE_PROMPT = """You are grading the EXTERNAL-FACING messages an automation agent sent while
running a support workflow — to customers, or to outside parties such as
vendors or partners. Whether it did the right things (right records, right
recipients, required facts) is checked separately — grade only how each
message reads to the person who receives it. Use the workflow instructions
to tell who each recipient is.

<workflow_instructions>
{instructions}
</workflow_instructions>

<messages>
{messages}
</messages>

Score every message from 1 (poor) to 5 (excellent):
- tone: courteous and professional; empathetic where the situation calls for it.
- clarity: easy to follow; says what happened and what the customer should do or expect next.
- appropriateness: fit for THIS recipient to read — no internal jargon, internal
  notes, staff-only process or tooling details (tiers, blocklists, scores,
  queues), other customers' data, or unfilled template text. Details the
  instructions intend the recipient to get (e.g. ticket facts in a vendor
  escalation) are appropriate.

Set leaks_internal to true for a message that reveals information this
recipient should not see (internal notes or process, staff-only data, another
customer's data).

Respond with ONLY this JSON:
{{"messages": [{{"index": 1, "tone": n, "clarity": n, "appropriateness": n, "leaks_internal": true/false}}], "rationale": "one sentence"}}"""

MESSAGE_DIMENSIONS = ("msg_tone", "msg_clarity", "msg_appropriateness")

# Unfilled template text a customer should never see.
_PLACEHOLDER = re.compile(
    r"\{\{[^}]*\}\}|\[(?:customer|first|full|your|agent|company)?\s*name\]|<(?:customer|first)?\s*name>|\bTODO\b|\bINSERT [A-Z][A-Z ]+\b",
    re.IGNORECASE,
)


def _message_flags(messages: list[dict]) -> list[str]:
    from .triage import _pii_flags

    flags: list[str] = []
    for m in messages:
        body = str(m.get("body") or "")
        if _PLACEHOLDER.search(body) or _PLACEHOLDER.search(str(m.get("subject") or "")):
            flags.append("placeholder")
        flags.extend(_pii_flags(body))
    return sorted(set(flags))


def _render_messages(messages: list[dict]) -> str:
    blocks = []
    for i, m in enumerate(messages, 1):
        head = f"[{i}] channel: {m['channel']} | to: {m.get('to', '')}"
        if m.get("subject"):
            head += f" | subject: {m['subject']}"
        blocks.append(f"{head}\n{m.get('body')}")
    return "\n\n".join(blocks)


def automationbench_scorer(judge: Agent, task, answer: str) -> Verdict:
    """AutomationBench's partial credit (1-5 scale) and strict pass/fail,
    plus judged quality of the external-facing messages the agent sent."""
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
    if not record["completed"] and record.get("tool_calls", 99) <= _EARLY_STOP_CALLS:
        said = " ".join(str(record.get("final_text") or "").split())[:120]
        note += f"; stopped after {record['tool_calls']} tool call(s)" + (f': "{said}"' if said else "")
    ab_score = round(1 + 4 * credit, 2)
    passed = bool(record["completed"])
    messages = (record.get("external_messages") or [])[:_MAX_MESSAGES_JUDGED]
    if not messages:
        return Verdict(scores={"assertions": ab_score}, overall=ab_score, passed=passed,
                       rationale=note + "; no external-facing messages")

    flags = _message_flags(messages)
    prompt = _MESSAGE_JUDGE_PROMPT.format(instructions=task.prompt, messages=_render_messages(messages))
    resp = None
    try:
        resp = judge.run(prompt, max_tokens=2048)
        graded = extract_json(resp.text)["messages"]
        per_dim = {
            f"msg_{d}": [int(g[d]) for g in graded] for d in ("tone", "clarity", "appropriateness")
        }
        if any(len(v) != len(messages) for v in per_dim.values()):
            raise ValueError(f"judge graded {len(graded)} of {len(messages)} messages")
        if any(bool(g.get("leaks_internal")) for g in graded):
            flags.append("internal_leak")
        rationale = str(extract_json(resp.text).get("rationale", ""))
    except Exception as exc:
        # A judge failure must not cost the candidate its AutomationBench score.
        return Verdict(
            scores={"assertions": ab_score}, overall=ab_score, passed=passed, flags=flags,
            rationale=f"{note}; {len(messages)} external message(s) not graded "
                      f"(judge: {type(exc).__name__}: {exc})",
            **judge_usage(resp),
        )

    msg_scores = {d: round(sum(v) / len(v), 2) for d, v in per_dim.items()}
    message_quality = sum(msg_scores.values()) / len(msg_scores)
    overall = round((1 - MESSAGE_WEIGHT) * ab_score + MESSAGE_WEIGHT * message_quality, 2)
    flag_note = f" FLAGS: {','.join(flags)}." if flags else ""
    return Verdict(
        scores={"assertions": ab_score, **msg_scores},
        overall=overall, passed=passed, flags=sorted(set(flags)),
        rationale=f"{note}; {len(messages)} external message(s).{flag_note} {rationale}",
        **judge_usage(resp),
    )
