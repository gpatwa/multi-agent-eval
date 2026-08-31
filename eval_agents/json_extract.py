r"""Extract a JSON object from an LLM's text response.

Judges are instructed to respond with JSON only, but not every model (local
7-8B models, less obedient judges) follows that reliably — real observed
failure modes include reasoning in prose before the JSON, or echoing input
context that itself contains braces. A naive greedy regex
(`re.search(r"\{.*\}", text, re.DOTALL)`) merges everything from the FIRST
`{` to the LAST `}` into one invalid blob in both cases, discarding an
otherwise-valid verdict. See tests/test_json_extract.py for reproductions.

This scans for balanced top-level `{...}` objects (respecting strings, so
braces inside quoted text don't confuse the depth count) and tries each
candidate, preferring the last one — models are instructed to put the JSON
verdict at the end, after any reasoning.
"""
from __future__ import annotations

import json
import re


def _iter_balanced_objects(text: str):
    """Yield every top-level {...} substring, matching braces correctly
    even when they appear inside string values."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        j = i
        while j < n:
            c = text[j]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i : j + 1]
                        break
            j += 1
        else:
            return  # unbalanced from here on; nothing more to find
        i = j + 1


def extract_json(text: str) -> dict:
    """Pull the JSON object a judge/candidate meant to return.

    Raises ValueError (not JSONDecodeError) when nothing parseable is found,
    so callers can catch one exception type.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip("` \n")

    # Fast path: fully compliant output, no scanning needed.
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fallback: try each balanced {...} span, last-to-first (the real
    # verdict is instructed to come last, after any reasoning/echoed input).
    candidates = list(_iter_balanced_objects(cleaned))
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"no JSON object found in judge output: {cleaned[:200]!r}")
