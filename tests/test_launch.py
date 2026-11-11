"""Announcement drafts are written from the published data, carry its limits, and fit their channels."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import xml.etree.ElementTree as ET

from eval_agents import history as H
from eval_agents.launch import HN_TITLE_MAX, X_POST_MAX, feed, findings, kit, limitations, model_names, readiness, x_posts
from tests.test_publish import _release

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _ready(tmp_path):
    data = _release(tmp_path)
    data["site_url"] = "https://eval.example.dev"
    # Pin the date: _release() stamps "now", which made tests that append a
    # later fixed-date entry fail once the wall clock passed that date.
    data["generated"] = "2026-10-01T00:00:00+00:00"
    hist, _ = H.append({"entries": []}, data)
    return data, hist


def test_nothing_is_announced_before_results_are_published_and_cross_checked(tmp_path):
    data, hist = _ready(tmp_path)
    assert readiness(data, hist) == []
    assert readiness({**data, "status": "pending"}, hist) == ["results are not published yet"]
    assert any("second-judge" in p for p in readiness({**data, "agreement": None}, hist))
    assert any("site URL" in p for p in readiness({**data, "site_url": ""}, hist))
    assert any("history is empty" in p for p in readiness(data, {"entries": []}))


def test_every_draft_names_the_models_links_the_site_and_states_the_limits(tmp_path):
    data, hist = _ready(tmp_path)
    files = kit(data, hist)
    assert set(files) == {"show_hn.md", "reddit.md", "x_thread.md", "linkedin.md", "blog_outline.md", "release_notes.md"}
    for name, text in files.items():
        assert all(m in text for m in model_names(data)), name
        assert "https://eval.example.dev" in text and "Cost is not scored in this run" in text, name
        assert "reasoning effort" in text, name


def test_drafts_fit_their_channels(tmp_path):
    data, hist = _ready(tmp_path)
    posts = x_posts(data, hist)
    assert len(posts) >= 5 and all(0 < len(p) <= X_POST_MAX for p in posts)
    title = next(ln for ln in kit(data, hist)["show_hn.md"].splitlines() if ln.startswith("**Title")).split(": ", 1)[1]
    assert len(title) <= HN_TITLE_MAX


def test_drafts_follow_the_data_not_a_template(tmp_path):
    data, hist = _ready(tmp_path)
    before = kit(data, hist)["linkedin.md"]
    flagged = copy.deepcopy(data)
    name = flagged["suites"]["public"]["ranking"][0]
    flagged["suites"]["public"]["candidates"][name]["critical_violations"] = 7
    after = kit(flagged, hist)["linkedin.md"]
    assert before != after
    total = sum(c["critical_violations"] for st in flagged["suites"].values() for c in st["candidates"].values())
    assert f"{total} confirmed flags" in " ".join(findings(flagged))


def test_cost_is_only_called_unscored_when_it_has_no_weight(tmp_path):
    data, _ = _ready(tmp_path)
    assert "Cost is not scored in this run." in limitations(data)
    data["suites"]["public"]["weights"] = {"quality": 0.7, "latency": 0.15, "cost": 0.15}
    assert "Cost is not scored in this run." not in limitations(data)


def test_feed_is_valid_atom_newest_first_with_stable_unique_ids(tmp_path):
    data, hist = _ready(tmp_path)
    later = copy.deepcopy(data)
    later["generated"] = "2026-10-05T00:00:00+00:00"
    later["suites"]["public"]["candidates"]["claude"]["critical_violations"] += 3
    hist, _ = H.append(hist, later)
    root = ET.fromstring(feed(data, hist))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    ids = [e.find("a:id", ns).text for e in entries]
    assert len(entries) == 2 and len(set(ids)) == 2
    assert entries[0].find("a:updated", ns).text > entries[1].find("a:updated", ns).text
    assert "flags" in entries[0].find("a:summary", ns).text
    assert feed(data, {"entries": []}) == ""  # nothing to publish yet


def test_launch_script_refuses_when_not_ready_and_builds_the_release_command(tmp_path):
    spec = importlib.util.spec_from_file_location("launch_kit", ROOT / "scripts" / "launch_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    data, _ = _ready(tmp_path)
    cmd = mod.release_command(data, tmp_path / "notes.md")
    assert cmd[:4] == ["gh", "release", "create", f"results-{data['generated'][:10]}"] and "--notes-file" in cmd
    # against the repo's real published data: drafts are written, nothing is posted
    assert mod.main(["--out", str(tmp_path / "kit")]) == 0
    assert (tmp_path / "kit" / "show_hn.md").read_text().startswith("<!-- generated from docs/data/results.json")


def test_a_judge_that_is_also_a_candidate_is_disclosed_up_front(tmp_path):
    data, hist = _ready(tmp_path)
    models = model_names(data)
    data["suites"]["public"]["judge"] = {**(data["suites"]["public"].get("judge") or {}), "model": models[0]}
    data["agreement"]["public"]["judge_second"] = {"provider": "x", "model": "unrelated-judge-9"}
    lim = " ".join(limitations(data))
    assert f"The primary judge ({models[0]}) is also a candidate." in lim and "Self-preference is possible" in lim
    for name in ("show_hn.md", "reddit.md", "linkedin.md", "release_notes.md"):
        assert "Self-preference is possible" in kit(data, hist)[name], name
    assert any("Self-preference" in p for p in x_posts(data, hist))
    # no overlap, no disclosure line
    data["suites"]["public"]["judge"]["model"] = "unrelated-judge-8"
    assert "Self-preference" not in " ".join(limitations(data))


def test_flags_are_attributed_to_the_model_that_raised_them(tmp_path):
    data, _ = _ready(tmp_path)
    for s in data["suites"].values():
        for c in s["candidates"].values():
            c["critical_violations"] = 0
    pub = data["suites"]["public"]["candidates"]
    name = next(iter(pub))
    pub[name]["critical_violations"] = 2
    line = findings(data)[-1]
    assert f"({pub[name]['model']}: 2)" in line and line.startswith("2 confirmed flags")


def test_latency_claim_carries_the_cli_startup_caveat_in_the_thread(tmp_path):
    data, hist = _ready(tmp_path)
    posts = x_posts(data, hist)
    slow = [p for p in posts if "slowest median latency" in p]
    assert all("startup" in p for p in slow)
    assert all(len(p) <= X_POST_MAX for p in posts)


def _launch_script():
    spec = importlib.util.spec_from_file_location("launch_kit", ROOT / "scripts" / "launch_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_release_is_a_draft_unless_publishing_is_asked_for(tmp_path):
    mod = _launch_script()
    data, _ = _ready(tmp_path)
    notes = tmp_path / "n.md"
    assert "--draft" in mod.release_command(data, notes) and "--draft" not in mod.release_command(data, notes, draft=False)
    assert mod.release_command(data, notes)[3] == f"results-{data['generated'][:10]}"


def test_an_existing_release_is_updated_if_draft_and_never_touched_if_public():
    mod = _launch_script()
    assert mod.release_action(None, publish=False) == "create"
    assert mod.release_action({"isDraft": True}, publish=False) == "update-draft"
    assert mod.release_action({"isDraft": True}, publish=True) == "publish-draft"
    assert mod.release_action({"isDraft": False}, publish=False) == "refuse"
    assert mod.release_action({"isDraft": False}, publish=True) == "refuse"


def test_release_body_is_public_text_only(tmp_path):
    data, hist = _ready(tmp_path)
    body = kit(data, hist)["release_notes.md"]
    assert body.startswith("## Results run") and "generated from" not in body and "Where:" not in body and "go-ahead" not in body
    assert all(m in body for m in model_names(data)) and "Cost is not scored in this run" in body
