"""Published history: one entry per distinct set of published results.

docs/data/history.json is append-only. The pipeline adds an entry when a publish
changes what the page reports (a model ID, effort, judge, score or flag count) and
adds nothing when it doesn't, so re-running is safe. Each entry records how it
differs from the one before it, so a model swap that lowered quality, or added
guardrail flags, is on the record rather than only in a diff.

Pure functions of the release data; no I/O beyond load/dump helpers.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

SCHEMA = 1
QUALITY_MOVE = 0.15  # judge points (1-5 scale) of quality_mean change that is worth recording
REGRESSIONS = ("quality_down", "flags_up")


def _judge(suite: dict) -> str | None:
    j = suite.get("judge") or {}
    return j.get("model")


def snapshot(data: dict) -> dict:
    """The facts a history row needs, from published release data."""
    suites = {}
    for name, s in data["suites"].items():
        suites[name] = {
            "n_tasks": s["n_tasks"],
            "ranking": s["ranking"],
            "judge": _judge(s),
            "candidates": {
                c: {"model": v["model"], "effort": v["effort"], "composite": v["composite"],
                    "quality_mean": v["quality_mean"], "latency_p50": v["latency_p50"],
                    "critical_violations": v["critical_violations"]}
                for c, v in s["candidates"].items()},
        }
    first = next(iter(data["suites"]))
    agreement = (data.get("agreement") or {}).get(first) or {}
    return {"published": data["generated"], "judge_second": (agreement.get("judge_second") or {}).get("model"),
            "rank_rho": agreement.get("rank_rho"), "suites": suites}


def fingerprint(entry: dict) -> str:
    """Identity of what an entry reports: everything except when it was published and its changes."""
    core = {k: v for k, v in entry.items() if k not in ("published", "changes")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def compare(prev: dict, cur: dict, quality_move: float = QUALITY_MOVE) -> list[dict]:
    """How `cur` differs from `prev`, per suite and candidate. Deterministic order."""
    changes = []
    for suite, s in cur["suites"].items():
        before = prev["suites"].get(suite)
        if before is None:
            continue
        if before["ranking"] and s["ranking"] and before["ranking"][0] != s["ranking"][0]:
            changes.append({"suite": suite, "candidate": "*", "kind": "leader_changed", "from": before["ranking"][0], "to": s["ranking"][0]})
        for name, now in s["candidates"].items():
            was = before["candidates"].get(name)
            if was is None:
                changes.append({"suite": suite, "candidate": name, "kind": "added"})
                continue
            if was["model"] != now["model"]:
                changes.append({"suite": suite, "candidate": name, "kind": "model_changed", "from": was["model"], "to": now["model"]})
            delta = round(now["quality_mean"] - was["quality_mean"], 3)
            if delta <= -quality_move:
                changes.append({"suite": suite, "candidate": name, "kind": "quality_down", "delta": delta})
            elif delta >= quality_move:
                changes.append({"suite": suite, "candidate": name, "kind": "quality_up", "delta": delta})
            if now["critical_violations"] > was["critical_violations"]:
                changes.append({"suite": suite, "candidate": name, "kind": "flags_up",
                                "from": was["critical_violations"], "to": now["critical_violations"]})
            elif now["critical_violations"] < was["critical_violations"]:
                changes.append({"suite": suite, "candidate": name, "kind": "flags_down",
                                "from": was["critical_violations"], "to": now["critical_violations"]})
        for name in before["candidates"]:
            if name not in s["candidates"]:
                changes.append({"suite": suite, "candidate": name, "kind": "removed"})
    if prev.get("judge_second") != cur.get("judge_second") or any(
            prev["suites"].get(n, {}).get("judge") != s.get("judge") for n, s in cur["suites"].items()):
        changes.append({"suite": "*", "candidate": "*", "kind": "judge_changed"})
    return changes


def regressions(changes: list[dict]) -> list[dict]:
    return [c for c in changes if c["kind"] in REGRESSIONS]


def append(history: dict, data: dict) -> tuple[dict, dict | None]:
    """(new history, the entry added or None when the results are unchanged)."""
    entries = list(history.get("entries", []))
    entry = snapshot(data)
    entry["id"] = fingerprint(entry)
    if entries and entries[-1]["id"] == entry["id"]:
        return {"schema": SCHEMA, "entries": entries}, None
    entry["changes"] = compare(entries[-1], entry) if entries else []
    return {"schema": SCHEMA, "entries": [*entries, entry]}, entry


def load(path: pathlib.Path) -> dict:
    path = pathlib.Path(path)
    return json.loads(path.read_text()) if path.exists() else {"schema": SCHEMA, "entries": []}


def dump(history: dict) -> str:
    return json.dumps(history, indent=2) + "\n"
