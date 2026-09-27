"""External-facing message grading for the AutomationBench lane: which tool
calls count as external-facing, and how judged message quality combines
with AutomationBench's own score. Needs no AutomationBench install."""
from __future__ import annotations

import json

import pytest

from eval_agents.agents import Agent
from eval_agents.providers.mock_provider import MockProvider
from eval_agents.runner import Task
from eval_agents.usecases.automationbench import MESSAGE_WEIGHT, automationbench_scorer, external_message

# ---------------------------------------------------------------- classification


def test_email_to_internal_only_is_not_external():
    assert external_message("gmail_send_email", {"to": "ops@company.example.com", "subject": "s", "body": "b"}) is None
    assert external_message("gmail_send_email", {"to": "lead@ourcompany.example.com", "body": "b"}) is None


def test_email_lists_only_external_recipients():
    m = external_message("gmail_send_email", {"to": "amy@acme.com, boss@company.example.com", "subject": "Hi", "body": "b"})
    assert m["channel"] == "email" and m["to"] == "amy@acme.com" and m["subject"] == "Hi"
    assert external_message("gmail_send_email", {"to": "boss@company.example.com", "cc": ["Amy <amy@acme.com>"], "body": "b"})["to"] == "amy@acme.com"


@pytest.mark.parametrize("tool,args,facing", [
    ("zendesk_add_comment_to_ticket", {"ticket_id": 1, "comment": "c"}, True),            # public by default
    ("zendesk_add_comment_to_ticket", {"ticket_id": 1, "comment": "c", "public": False}, False),
    ("zendesk_add_comment_to_ticket", {"ticket_id": 1, "comment": "c", "public": "false"}, False),
    ("zoho_desk_add_comment", {"ticket_id": 1, "content": "c"}, True),
    ("zoho_desk_add_comment", {"ticket_id": 1, "content": "c", "is_public": False}, False),
    ("freshdesk_add_note_to_ticket", {"ticket_id": 1, "body": "c"}, False),                # private by default
    ("freshdesk_add_note_to_ticket", {"ticket_id": 1, "body": "c", "private": False}, True),
    ("reamaze_add_message", {"conversation_id": 1, "body": "c"}, True),
    ("reamaze_add_message", {"conversation_id": 1, "body": "c", "visibility": "internal"}, False),
    ("gorgias_create_ticket_message", {"ticket_id": 1, "body_text": "c"}, True),
    ("gorgias_create_ticket_message", {"ticket_id": 1, "body_text": "c", "channel": "internal-note"}, False),
    ("gorgias_create_ticket_message", {"ticket_id": 1, "body_text": "c", "sender_type": "customer"}, False),
    ("intercom_reply_to_conversation", {"conversation_id": 1, "body": "c"}, True),
    ("intercom_reply_to_conversation", {"conversation_id": 1, "body": "c", "author_type": "user"}, False),
    ("intercom_add_note", {"conversation_id": 1, "body": "c"}, False),
    ("helpscout_send_reply", {"conversation_id": 1, "body": "c"}, True),
    ("helpscout_add_note", {"conversation_id": 1, "body": "c"}, False),
    ("slack_send_channel_message", {"channel": "#x", "text": "c"}, False),
    ("gmail_create_draft", {"to": "amy@acme.com", "body": "c"}, False),                    # never sent
])
def test_visibility_rules(tool, args, facing):
    assert (external_message(tool, args) is not None) is facing


# ---------------------------------------------------------------- scoring


def _task():
    return Task(id="support.x", category="automationbench/support", prompt="Reply to each customer about their refund.")


def _answer(credit=0.5, completed=False, messages=()):
    return json.dumps({
        "partial_credit": credit, "completed": completed, "assertions_passed": 5, "assertions_scored": 10,
        "failed": [], "tool_calls": 6, "tool_errors": 0, "cut_off": False, "external_messages": list(messages),
    })


MSG = {"channel": "email", "to": "amy@acme.com", "subject": "Your refund", "body": "Hi Amy, your refund is on its way."}


def _judge_json(*rows, rationale="ok"):
    return json.dumps({"messages": [
        {"index": i, "tone": t, "clarity": c, "appropriateness": a, "leaks_internal": leak}
        for i, (t, c, a, leak) in enumerate(rows, 1)
    ], "rationale": rationale})


def test_no_external_messages_means_no_judge_call(fake_judge):
    judge = fake_judge(response_text=_judge_json())
    v = automationbench_scorer(judge, _task(), _answer(credit=0.5))
    assert not judge.provider.calls
    assert v.overall == 3.0 and v.scores == {"assertions": 3.0}


def test_message_quality_blends_into_overall_but_not_pass_fail(fake_judge):
    judge = fake_judge(response_text=_judge_json((5, 4, 3, False), (3, 4, 5, False)), input_tokens=900, output_tokens=60)
    v = automationbench_scorer(judge, _task(), _answer(credit=1.0, completed=True, messages=[MSG, MSG]))
    assert v.scores == {"assertions": 5.0, "msg_tone": 4.0, "msg_clarity": 4.0, "msg_appropriateness": 4.0}
    assert v.overall == round((1 - MESSAGE_WEIGHT) * 5.0 + MESSAGE_WEIGHT * 4.0, 2)
    assert v.passed is True  # AutomationBench's verdict is never altered
    assert (v.judge_input_tokens, v.judge_output_tokens) == (900, 60)
    prompt = judge.provider.calls[0][-1].content
    assert "[1] channel: email | to: amy@acme.com | subject: Your refund" in prompt
    assert "Reply to each customer about their refund." in prompt


def test_internal_leak_flagged(fake_judge):
    judge = fake_judge(response_text=_judge_json((4, 4, 1, True)))
    v = automationbench_scorer(judge, _task(), _answer(messages=[MSG]))
    assert "internal_leak" in v.flags


def test_placeholder_and_pii_flagged_deterministically(fake_judge):
    bad = {**MSG, "body": "Hi {{first_name}}, card 4111 1111 1111 1111 is refunded."}
    v = automationbench_scorer(fake_judge(response_text=_judge_json((4, 4, 4, False))), _task(), _answer(messages=[bad]))
    assert {"placeholder", "pii_echo"} <= set(v.flags)


def test_judge_failure_keeps_automationbench_score(fake_judge):
    judge = fake_judge(raises=RuntimeError("rate limited"))
    v = automationbench_scorer(judge, _task(), _answer(credit=0.75, messages=[MSG]))
    assert v.parse_error is None  # still counts toward quality
    assert v.overall == 4.0 and v.scores == {"assertions": 4.0}
    assert "not graded" in v.rationale


def test_judge_grading_wrong_number_of_messages_is_a_failure(fake_judge):
    judge = fake_judge(response_text=_judge_json((5, 5, 5, False)))
    v = automationbench_scorer(judge, _task(), _answer(credit=0.75, messages=[MSG, MSG]))
    assert v.overall == 4.0 and "not graded" in v.rationale


def test_mock_judge_speaks_the_message_rubric():
    judge = Agent(name="judge", provider=MockProvider("mock-judge"))
    v = automationbench_scorer(judge, _task(), _answer(credit=1.0, completed=True, messages=[MSG, MSG, MSG]))
    assert set(v.scores) == {"assertions", "msg_tone", "msg_clarity", "msg_appropriateness"}
