"""Published history: append-only, idempotent, and honest about what changed between runs."""
from __future__ import annotations

import copy
import json
import pathlib

from eval_agents import history as H
from eval_agents.site import REGION, generated_files, render
from tests.test_publish import _release

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = (ROOT / "docs" / "index.html").read_text()


def _regions(html: str) -> str:
    return "\n".join(m.group(3) for m in REGION.finditer(html))


def _later(data: dict, **edits) -> dict:
    """The same release with one candidate's public-suite numbers edited."""
    d = copy.deepcopy(data)
    d["generated"] = "2026-10-01T00:00:00+00:00"
    for key, value in edits.items():
        name, field = key.split("__")
        d["suites"]["public"]["candidates"][name][field] = value
    return d


def test_first_publish_creates_one_entry_with_no_changes(tmp_path):
    data = _release(tmp_path)
    hist, entry = H.append({"entries": []}, data)
    assert entry and len(hist["entries"]) == 1 and entry["changes"] == [] and entry["id"]


def test_republishing_identical_results_adds_nothing(tmp_path):
    data = _release(tmp_path)
    hist, _ = H.append({"entries": []}, data)
    again, entry = H.append(hist, {**data, "generated": "2027-01-01T00:00:00+00:00"})  # only the date differs
    assert entry is None and again == hist


def test_model_swap_and_quality_drop_and_new_flags_are_recorded(tmp_path):
    data = _release(tmp_path)
    hist, _ = H.append({"entries": []}, data)
    base = data["suites"]["public"]["candidates"]["claude"]
    new = _later(data, claude__model="claude-next", claude__quality_mean=round(base["quality_mean"] - 0.5, 3),
                 claude__critical_violations=base["critical_violations"] + 2)
    hist, entry = H.append(hist, new)
    kinds = {(c["candidate"], c["kind"]) for c in entry["changes"]}
    assert {("claude", "model_changed"), ("claude", "quality_down"), ("claude", "flags_up")} <= kinds
    assert [c["kind"] for c in H.regressions(entry["changes"])] == ["quality_down", "flags_up"]
    assert len(hist["entries"]) == 2  # append-only: the first entry is untouched
    assert hist["entries"][0]["changes"] == []


def test_small_moves_are_not_noise(tmp_path):
    data = _release(tmp_path)
    base = H.snapshot(data)
    moved = H.snapshot(_later(data, gpt__quality_mean=data["suites"]["public"]["candidates"]["gpt"]["quality_mean"] - 0.05))
    assert H.compare(base, moved) == []


def test_improvements_are_recorded_but_are_not_regressions(tmp_path):
    data = _release(tmp_path)
    data["suites"]["public"]["candidates"]["gpt"]["quality_mean"] = 3.0  # room to improve
    changes = H.compare(H.snapshot(data), H.snapshot(_later(data, gpt__quality_mean=4.0)))
    assert H.regressions(changes) == [] and [c["kind"] for c in changes] == ["quality_up"]


def _history_region(html: str) -> str:
    return next(m.group(3) for m in REGION.finditer(html) if m.group(2) == "history")


def test_page_renders_history_newest_first_with_regressions_marked(tmp_path):
    data = _release(tmp_path)
    hist, _ = H.append({"entries": []}, data)
    hist, _ = H.append(hist, _later(data, claude__critical_violations=9))
    out = _history_region(render(PAGE, {**data, "history": hist}))
    body = out.split("<tbody>")[1]
    assert body.count("<tr>") == 2
    assert body.index("2026-10-01") < body.index("first published run")  # newest first
    assert 'class="chg bad">claude: flags' in out


def test_no_history_says_so_without_claims():
    out = _regions(render(PAGE, {"schema": 1, "status": "pending", "reason": "x", "providers": 19,
                                 "suites": {"public": {"n_tasks": 1, "n_guardrail": 0}}}))
    assert "first row appears here" in out


def test_generated_files_read_history_from_disk(tmp_path):
    for rel in ("docs/data", "docs"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "index.html").write_text(PAGE)
    data = _release(tmp_path)
    hist, _ = H.append({"entries": []}, data)
    (tmp_path / "docs" / "data" / "history.json").write_text(H.dump(hist))
    page = dict(generated_files(tmp_path, data))[tmp_path / "docs" / "index.html"]
    assert "first published run" in page
