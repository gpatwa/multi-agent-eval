"""Regression tests for eval_agents.json_extract.

Both failure modes here are real, observed judge output shapes (chatty
local/open-weight models), not hypotheticals — see json_extract.py's
docstring. Before the fix, both raised JSONDecodeError from a greedy regex
that spanned from the first '{' to the last '}' in the whole response.
"""
from __future__ import annotations

import pytest

from eval_agents.json_extract import extract_json


def test_clean_json_parses():
    text = '{"scores": {"a": 5}, "rationale": "ok"}'
    assert extract_json(text) == {"scores": {"a": 5}, "rationale": "ok"}


def test_code_fence_stripped():
    text = '```json\n{"scores": {"a": 5}}\n```'
    assert extract_json(text) == {"scores": {"a": 5}}


def test_chatty_preamble_with_stray_brace():
    """Model reasons in prose (containing an unquoted '{...}' fragment)
    before the real JSON verdict. Greedy regex used to merge both into one
    invalid blob."""
    text = (
        "Let me analyze this reply against the policy.\n\n"
        "The reply correctly identifies the ticket as {billing, normal} in "
        "terms of category, and I'll evaluate the actual response text now.\n\n"
        '{"scores": {"policy_adherence": 4, "resolution": 5, "tone": 4}, '
        '"critical_violation": false, "rationale": "Handles refund correctly."}'
    )
    result = extract_json(text)
    assert result["scores"]["policy_adherence"] == 4
    assert result["rationale"] == "Handles refund correctly."


def test_echoed_context_with_valid_json_braces():
    """Model echoes ticket context that is itself valid JSON before
    answering. Greedy regex used to concatenate both objects -> 'Extra
    data' JSONDecodeError. The real answer (last object) must win."""
    text = (
        'Ticket context: {"category": "billing", "priority": "normal"}\n\n'
        "My evaluation:\n"
        '{"scores": {"policy_adherence": 5, "resolution": 5, "tone": 5}, '
        '"critical_violation": false, "rationale": "Good."}'
    )
    result = extract_json(text)
    assert result["scores"]["policy_adherence"] == 5
    assert "category" not in result  # must not return the echoed context


def test_brace_inside_string_value_does_not_break_depth_counting():
    text = '{"rationale": "the format was like {this} in the reply", "score": 3}'
    assert extract_json(text) == {
        "rationale": "the format was like {this} in the reply",
        "score": 3,
    }


def test_no_json_raises_value_error():
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_json("I refuse to answer in JSON.")


def test_unbalanced_braces_raises_value_error():
    with pytest.raises(ValueError):
        extract_json("{not even close to json")
