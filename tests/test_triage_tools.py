"""Tests for the tool-use triage tier: sandbox validation and end-state
grading, the tool loop, the scorer, candidate filtering, the vendor
adapters' message wiring (with stub clients — no network), and the
static integrity of tasks.triage.tools.yaml."""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

import pytest

from eval_agents.agents import Agent
from eval_agents.config import load_agents, load_tasks
from eval_agents.providers.anthropic_provider import AnthropicProvider
from eval_agents.providers.base import ChatMessage, Provider, ToolCall, ToolResult, ToolTurn
from eval_agents.providers.openai_provider import OpenAIProvider
from eval_agents.runner import Task
from eval_agents.usecases import triage_tools as tt
from eval_agents.usecases.triage import CATEGORIES, PRIORITIES

ROOT = pathlib.Path(__file__).resolve().parent.parent

ACCOUNT = {
    "email": "sam@example.com",
    "plan": "monthly",
    "charges": [{"id": "ch_2", "amount_usd": 49, "days_ago": 3}, {"id": "ch_1", "amount_usd": 49, "days_ago": 3}],
    "cancellation": None,
}


def _call(name, **args):
    return ToolCall(id=f"id-{name}", name=name, arguments=args)


def _task(gold, prompt="From: sam@example.com\nbilled twice"):
    return Task(id="t", category="ticket", prompt=prompt, reference="ref", gold=gold, fixture={"account": ACCOUNT})


# ---------------------------------------------------------------- sandbox


def test_lookup_requires_matching_email():
    box = tt.TicketSandbox({"account": ACCOUNT})
    assert box.execute(_call("lookup_account", email="nobody@example.com")).is_error
    ok = box.execute(_call("lookup_account", email="SAM@example.com "))
    assert not ok.is_error and json.loads(ok.content)["plan"] == "monthly"


def test_refund_validation():
    box = tt.TicketSandbox({"account": ACCOUNT})
    assert box.execute(_call("issue_refund", charge_id="ch_999", type="full")).is_error
    assert box.execute(_call("issue_refund", charge_id="ch_1", type="half")).is_error
    assert not box.execute(_call("issue_refund", charge_id="ch_1", type="duplicate_charge")).is_error
    assert box.execute(_call("issue_refund", charge_id="ch_1", type="duplicate_charge")).is_error  # already refunded
    assert box.state()["refund_count"] == 1


def test_bad_arguments_and_unknown_tools_are_errors_not_crashes():
    box = tt.TicketSandbox({"account": ACCOUNT})
    assert box.execute(_call("set_triage", category="billing")).is_error          # missing priority
    assert box.execute(_call("set_triage", category="sales", priority="high")).is_error
    assert box.execute(ToolCall(id="x", name="send_reply", arguments={"_invalid_json": "{"})).is_error
    assert box.execute(_call("delete_account")).is_error
    assert len(box.trace) == 4 and all(t["error"] for t in box.trace)


def test_state_normalization():
    box = tt.TicketSandbox({"account": ACCOUNT})
    for c in [
        _call("set_triage", category="billing", priority="high"),
        _call("issue_refund", charge_id="ch_2", type="duplicate_charge"),
        _call("escalate", team="security"),
        _call("escalate", team="security"),
        _call("send_reply", message="first"),
        _call("send_reply", message="final"),
    ]:
        box.execute(c)
    s = box.state()
    assert s["refund"] == "duplicate_charge" and s["refund_charges"] == ["ch_2"]
    assert s["escalate"] == "security" and s["offer_pause"] is False and s["cancel"] == "none"
    assert s["reply"] == "final"


def test_grade_state():
    state = {"refund": "duplicate_charge", "refund_charges": ["ch_2"], "refund_count": 1,
             "escalate": "none", "offer_pause": False, "cancel": "none"}
    assert tt.grade_state(state, {"refund": "duplicate_charge", "refund_count": 1}) == (5, [])
    assert tt.grade_state(state, {"refund": "none", "escalate": "none"}) == (3, ["refund"])
    assert tt.grade_state(state, {"refund_charges": ["ch_1"]}) == (1, ["refund_charges"])
    assert tt.grade_state(state, {"offer_pause": True}) == (1, ["offer_pause"])
    assert tt.grade_state(state, {}) == (None, [])


# ---------------------------------------------------------------- loop


class ScriptedToolProvider(Provider):
    supports_tools = True

    def __init__(self, turns):
        super().__init__("scripted")
        self.turns = list(turns)
        self.histories = []

    def complete(self, messages, system=None, max_tokens=4096):
        raise AssertionError("tool tier must not use plain complete()")

    def complete_with_tools(self, history, tools, system=None, max_tokens=4096):
        self.histories.append(list(history))
        calls = self.turns.pop(0) if self.turns else [_call("lookup_account", email="sam@example.com")]
        return ToolTurn(text="" if calls else "done", model="scripted", tool_calls=calls,
                        input_tokens=10, output_tokens=2)


def test_run_candidate_executes_tools_and_records_state():
    provider = ScriptedToolProvider([
        [_call("lookup_account", email="sam@example.com")],
        [_call("set_triage", category="billing", priority="high"),
         _call("issue_refund", charge_id="ch_2", type="duplicate_charge")],
        [_call("send_reply", message="Refunded the duplicate.")],
        [],
    ])
    resp = tt.run_candidate(Agent(name="c", provider=provider, system="sys"), _task({}))
    record = json.loads(resp.text)
    assert record["state"]["refund_charges"] == ["ch_2"]
    assert [t["tool"] for t in record["trace"]] == ["lookup_account", "set_triage", "issue_refund", "send_reply"]
    assert record["cut_off"] is False
    assert (resp.input_tokens, resp.output_tokens) == (40, 8)
    # the second turn saw the first turn and its tool results
    assert isinstance(provider.histories[1][1], ToolTurn)
    assert isinstance(provider.histories[1][2][0], ToolResult)


def test_run_candidate_stops_at_step_limit():
    provider = ScriptedToolProvider([])  # calls a tool forever
    record = json.loads(tt.run_candidate(Agent(name="c", provider=provider), _task({})).text)
    assert record["cut_off"] is True
    assert len(record["trace"]) == tt.MAX_STEPS


# ---------------------------------------------------------------- scorer


def _record(state_overrides, trace=None):
    state = {"category": "billing", "priority": "high", "refund": "duplicate_charge", "refund_charges": ["ch_2"],
             "refund_count": 1, "escalate": "none", "offer_pause": False, "cancel": "none", "reply": "Done!"}
    state.update(state_overrides)
    trace = trace if trace is not None else [{"tool": "lookup_account", "args": {}, "result": "{}", "error": False}]
    return json.dumps({"state": state, "trace": trace, "final_text": "", "cut_off": False})


def _judge_ok():
    return json.dumps({"scores": {"policy_adherence": 5, "resolution": 5, "tone": 5},
                       "critical_violation": False, "contradicts_actions": False, "rationale": "ok"})


GOLD = {"category": "billing", "priority": "high", "state": {"refund": "duplicate_charge", "refund_count": 1}}


def test_scorer_perfect_run(fake_judge):
    judge = fake_judge(response_text=_judge_ok())
    v = tt.tools_scorer(judge, _task(GOLD), _record({}))
    assert v.scores["actions"] == 5 and v.overall == 5.0
    prompt = judge.provider.calls[0][-1].content
    assert '"refund": "duplicate_charge"' in prompt  # judge sees the EXECUTED actions


def test_scorer_wrong_action_and_no_lookup(fake_judge):
    v = tt.tools_scorer(fake_judge(response_text=_judge_ok()), _task(GOLD),
                        _record({"refund": "none", "refund_charges": [], "refund_count": 0}, trace=[]))
    assert v.scores["actions"] == 1
    assert "state miss: refund,refund_count" in v.rationale
    assert "never looked up the account" in v.rationale


def test_scorer_no_reply_is_flagged_without_judge(fake_judge):
    judge = fake_judge(response_text=_judge_ok())
    v = tt.tools_scorer(judge, _task(GOLD), _record({"reply": None}))
    assert "no_reply" in v.flags
    assert v.scores["policy_adherence"] == v.scores["resolution"] == v.scores["tone"] == 1
    assert not judge.provider.calls


def test_scorer_unreadable_answer(fake_judge):
    v = tt.tools_scorer(fake_judge(response_text=_judge_ok()), _task(GOLD), "not json")
    assert v.overall == 1.0


# ---------------------------------------------------------------- candidate filtering


class NoToolsProvider(Provider):
    def complete(self, messages, system=None, max_tokens=4096):
        raise AssertionError


def test_tool_use_case_skips_providers_without_tool_support(monkeypatch):
    import eval_agents.config as cfg

    def fake_create(provider, model):
        return NoToolsProvider(model) if provider == "notools" else ScriptedToolProvider([])

    monkeypatch.setattr(cfg, "create_provider", fake_create)
    config = {
        "use_case": "support_triage_tools",
        "candidates": [{"name": "a", "provider": "notools", "model": "x"}, {"name": "b", "provider": "tools", "model": "y"}],
        "judge": {"provider": "notools", "model": "j"},  # the judge only needs text
    }
    candidates, judge = load_agents(config)
    assert [c.name for c in candidates] == ["b"]
    assert isinstance(judge.provider, NoToolsProvider)


# ---------------------------------------------------------------- adapters (stub clients)


def _history_after_one_call(native):
    return [
        ChatMessage(role="user", content="ticket"),
        ToolTurn(text="", model="m", tool_calls=[_call("lookup_account", email="a@b.c")], native=native),
        [ToolResult(call_id="id-lookup_account", name="lookup_account", content="{...}", is_error=False)],
    ]


def test_openai_adapter_wire_format():
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        call = SimpleNamespace(id="c9", function=SimpleNamespace(name="send_reply", arguments='{"message": "hi"}'))
        msg = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], model="gpt-x",
                               usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))

    p = OpenAIProvider.__new__(OpenAIProvider)
    p.model = "gpt-x"
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    native = {"role": "assistant", "content": "", "tool_calls": [{"id": "id-lookup_account"}]}
    turn = p.complete_with_tools(_history_after_one_call(native), tt.TOOLS, system="sys")

    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["messages"][2] is native
    assert sent["messages"][3] == {"role": "tool", "tool_call_id": "id-lookup_account", "content": "{...}"}
    assert sent["tools"][0]["function"]["name"] == "lookup_account"
    assert turn.tool_calls[0].arguments == {"message": "hi"}
    assert turn.native["tool_calls"][0]["id"] == "c9"


def test_openai_adapter_bad_argument_json_becomes_sandbox_error():
    from eval_agents.providers.openai_provider import _parse_args

    args = _parse_args('{"message": ')
    assert "_invalid_json" in args
    assert tt.TicketSandbox({}).execute(ToolCall(id="x", name="send_reply", arguments=args)).is_error


def test_anthropic_adapter_wire_format():
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        blocks = [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="ok"),
                  SimpleNamespace(type="tool_use", id="tu1", name="escalate", input={"team": "security"})]
        return SimpleNamespace(content=blocks, model="claude-opus-5",
                               usage=SimpleNamespace(input_tokens=11, output_tokens=4))

    p = AnthropicProvider.__new__(AnthropicProvider)
    p.model = "claude-opus-5"
    p.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    native = ["<thinking block>", "<tool_use block>"]
    turn = p.complete_with_tools(_history_after_one_call(native), tt.TOOLS, system="sys")

    assert sent["messages"][1] == {"role": "assistant", "content": native}  # echoed unchanged
    assert sent["messages"][2]["role"] == "user"
    assert sent["messages"][2]["content"][0]["type"] == "tool_result"
    assert sent["messages"][2]["content"][0]["tool_use_id"] == "id-lookup_account"
    assert sent["tools"][0]["input_schema"]["required"] == ["email"]
    assert turn.text == "ok" and turn.tool_calls[0].name == "escalate"


# ---------------------------------------------------------------- task file


def test_tools_task_file_integrity():
    tasks = load_tasks(ROOT / "tasks.triage.tools.yaml")
    assert len({t.id for t in tasks}) == len(tasks)
    for t in tasks:
        assert t.gold["category"] in CATEGORIES and t.gold["priority"] in PRIORITIES, t.id
        account = t.fixture["account"]
        m = re.search(r"From:\s*(\S+)", t.prompt)
        assert m and m.group(1) == account["email"], f"{t.id}: From: line must match the fixture account"
        charge_ids = {c["id"] for c in account["charges"]}
        state = t.gold["state"]
        assert set(state) <= set(tt.STATE_KEYS), f"{t.id}: unknown state keys {set(state) - set(tt.STATE_KEYS)}"
        assert set(state.get("refund_charges", [])) <= charge_ids, f"{t.id}: refund_charges not on the account"
        if "refund" in state:
            assert state["refund"] in ["none", *tt.REFUND_TYPES], t.id
        if "escalate" in state:
            assert state["escalate"] in ["none", *tt.TEAMS], t.id
        if "cancel" in state:
            assert state["cancel"] in ["none", *tt.CANCEL_WHEN], t.id


def test_gemini_adapter_wire_format():
    from google.genai import types

    from eval_agents.providers.gemini_provider import GeminiProvider

    sent = {}

    def generate_content(**kwargs):
        sent.update(kwargs)
        content = types.Content(role="model", parts=[
            types.Part(text="thinking...", thought=True),
            types.Part(function_call=types.FunctionCall(name="set_triage", args={"category": "billing", "priority": "high"})),
        ])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)],
                               usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=2))

    p = GeminiProvider.__new__(GeminiProvider)
    p.model = "gemini-3.1-pro-preview"
    p.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    native = types.Content(role="model", parts=[types.Part(text="prior")])
    history = _history_after_one_call(native)
    history[1].tool_calls[0].id = "gem-7"
    history[2][0].call_id = "gem-7"
    turn = p.complete_with_tools(history, tt.TOOLS, system="sys")

    contents = sent["contents"]
    assert contents[1] is native  # echoed unchanged (thought signatures)
    fr = contents[2].parts[0].function_response
    assert (fr.id, fr.name, fr.response) == ("gem-7", "lookup_account", {"result": "{...}"})
    assert sent["config"].automatic_function_calling.disable is True
    assert turn.text == ""  # thought parts are not reply text
    assert turn.tool_calls[0].arguments == {"category": "billing", "priority": "high"}
    assert turn.tool_calls[0].id.startswith("local:")  # Gemini gave no id


def test_gemini_synthetic_call_ids_are_not_echoed():
    from eval_agents.providers.gemini_provider import GeminiProvider

    sent = {}
    p = GeminiProvider.__new__(GeminiProvider)
    p.model = "g"
    p.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: sent.update(kw) or
                               SimpleNamespace(candidates=[], usage_metadata=None)))
    history = _history_after_one_call(None)
    history[2][0].call_id = "local:lookup_account-0"
    turn = p.complete_with_tools(history, tt.TOOLS)
    assert sent["contents"][2].parts[0].function_response.id is None
    assert turn.tool_calls == [] and turn.native is not None  # empty candidate list handled
