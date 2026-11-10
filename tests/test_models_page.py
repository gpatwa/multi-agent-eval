"""The Models page: sourced rows only, vendor claims kept apart from measured results, refresh parsers."""
import copy
import json
import pathlib

import pytest

from eval_agents import models_page as mp
from eval_agents import refresh_models as rm

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOOD = {"schema": 1, "checked": "2026-11-10",
        "models": [{"vendor": "Anthropic", "name": "Claude Opus 5.5", "id": "claude-opus-5-5", "status": "latest",
                    "released": "2026-09-22", "price": "$4 / $20", "source": "https://platform.claude.com/x"}],
        "claims": [{"title": "T", "vendor": "Anthropic", "date": "2026-11-09", "source": "https://www.anthropic.com/x",
                    "models": ["A", "B"], "rows": [{"benchmark": "Bench", "area": "x", "scores": {"A": "1%"}}], "caveats": ["c"]}]}


def test_committed_models_json_validates():
    mp.validate(json.loads((ROOT / "docs" / "data" / "models.json").read_text()))


def test_a_model_without_a_source_is_rejected():
    bad = copy.deepcopy(GOOD)
    bad["models"][0]["source"] = ""
    with pytest.raises(mp.ModelsDataError, match="source"):
        mp.validate(bad)


def test_a_score_for_a_model_not_in_the_table_is_rejected():
    bad = copy.deepcopy(GOOD)
    bad["claims"][0]["rows"][0]["scores"]["Z"] = "9%"
    with pytest.raises(mp.ModelsDataError, match="not in the table"):
        mp.validate(bad)


def test_tested_badge_comes_from_the_published_run_not_the_data():
    pub = {"status": "published", "suites": {"public": {"candidates": {"c": {"model": "claude-opus-5-5"}}}}}
    assert "tested here" in mp.models_section(GOOD, pub)
    assert "tested here" not in mp.models_section(GOOD, {"status": "pending"})


def test_claims_are_labelled_as_vendor_reported_and_say_we_did_not_run_them():
    out = mp.claims_section(GOOD)
    assert "reported by Anthropic" in out and "We did not run them" in out


def test_missing_score_renders_a_dash_not_a_zero():
    assert "<td>—</td>" in mp.claims_section(GOOD)


ANTHROPIC_MD = """---
title: Claude Fable 5.2
---

**Latest.** Released November 9, 2026.

Model ID: `claude-fable-5-2`

Context window: 1M tokens · Max output: 128K tokens · Input pricing: $8 / MTok · Output pricing: $40 / MTok
[x](/docs/en/models/mythos-5-2/overview)
"""


def test_parse_anthropic_reads_id_status_date_and_price():
    r = rm.parse_anthropic(ANTHROPIC_MD, "fable-5-2")
    assert (r["id"], r["status"], r["released"], r["price"]) == ("claude-fable-5-2", "latest", "2026-11-09", "$8 / $40")


def test_parse_anthropic_handles_from_prices_and_restricted_models():
    md = ANTHROPIC_MD.replace("**Latest.**", "**Verification required.**").replace("$8 / MTok", "From $0.10 / MTok").replace("$40 / MTok", "From $0.50 / MTok")
    r = rm.parse_anthropic(md, "m")
    assert r["status"] == "restricted" and r["price"] == "from $0.10 / from $0.50"


def test_an_unknown_lifecycle_label_is_not_guessed():
    assert rm.parse_anthropic(ANTHROPIC_MD.replace("Latest", "Sunsetting"), "x") is None


def test_fetch_follows_links_and_reports_unreadable_pages():
    def get(url):
        if "mythos-5-2" in url:
            raise OSError("down")
        return ANTHROPIC_MD
    rows, problems = rm.fetch_anthropic(get)
    assert {r["id"] for r in rows} == {"claude-fable-5-2"} and any("mythos-5-2" in p for p in problems)


def test_merge_replaces_known_ids_and_keeps_unreachable_rows():
    old = [{"vendor": "Anthropic", "id": "a", "v": 1}, {"vendor": "Anthropic", "id": "gone", "v": 1}, {"vendor": "Google", "id": "g"}]
    out = rm.merge_anthropic(old, [{"vendor": "Anthropic", "id": "a", "v": 2}, {"vendor": "Anthropic", "id": "new", "v": 1}])
    assert [(m["id"], m.get("v")) for m in out] == [("a", 2), ("gone", 1), ("g", None), ("new", 1)]


def test_watch_ignores_assets_old_generations_and_known_ids_but_flags_new_flagships():
    page = "gpt-6-astra gpt-6.1-sol gpt-7-nova gpt-6-cyber.png gpt-realtime-2 gpt-5.6-cyber gpt-3-babbage gpt-6-astra-texture.webp"
    known = [{"id": "gpt-6-astra"}, {"id": "gpt-6.1-sol"}]
    unrev, problems = rm.watch(known, get=lambda url: page if "openai" in url else "gemini-4-argon")
    assert unrev["OpenAI"] == ["gpt-5.6-cyber", "gpt-7-nova"] and not problems


def test_an_unreadable_vendor_page_is_a_problem_not_a_clean_bill():
    unrev, problems = rm.watch([], get=lambda url: "<html>no models here</html>")
    assert not unrev and len(problems) == 2


def test_models_page_is_generated_and_in_the_sitemap():
    from eval_agents.site import generated_files
    data = json.loads((ROOT / "docs" / "data" / "results.json").read_text())
    files = {p.name: t for p, t in generated_files(ROOT, data)}
    assert "models.html" in files and "/models</loc>" in files["sitemap.xml"]
    assert files["models.html"].count('href="/models"') == 1  # one nav link, not two


def test_unreviewed_ids_are_shown_not_hidden():
    out = mp.models_section({**GOOD, "unreviewed": {"OpenAI": ["gpt-7-nova"]}}, {"status": "pending"})
    assert "not yet reviewed here: gpt-7-nova" in out
