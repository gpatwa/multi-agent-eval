"""The landing page is a pure function of docs/data/results.json, and states only what that data supports."""
from __future__ import annotations

import json
import pathlib
from html.parser import HTMLParser

import pytest

from eval_agents.judge import Verdict
from eval_agents.site import REGION, TITLE, generated_files, head, render, render_readme, robots, sitemap
from tests.test_publish import CANDS, JUDGE_B, _release, make_run

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = (ROOT / "docs" / "index.html").read_text()
DATA = json.loads((ROOT / "docs" / "data" / "results.json").read_text())


def regions(html: str) -> str:
    return "\n".join(m.group(3) for m in REGION.finditer(html))


class _Balance(HTMLParser):
    VOID = {"meta", "link", "br", "img", "input", "hr", "line", "path", "circle", "rect", "text"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
        else:
            self.stack.pop()


def assert_well_formed(fragment: str):
    p = _Balance()
    p.feed(fragment)
    assert not p.errors and not p.stack, (p.errors, p.stack)


def test_committed_page_is_the_render_of_the_committed_data():
    assert render(PAGE, DATA) == PAGE, "run: python scripts/build_site.py"


def test_render_is_idempotent_and_requires_every_region():
    assert render(render(PAGE, DATA), DATA) == render(PAGE, DATA)
    with pytest.raises(KeyError, match="missing AUTO region"):
        render(PAGE.replace("<!-- AUTO:stats -->", "").replace("<!-- /AUTO:stats -->", ""), DATA)


def test_pending_page_makes_no_result_claims():
    out = regions(render(PAGE, {**DATA, "status": "pending"}))
    for claim in ("Zero violations", "Spearman", "composite score", "ranks", "cross-checked", "held-out gap"):
        assert claim.lower() not in out.lower(), claim
    assert "being refreshed" in out and "Every run reports how it was judged" in out
    assert_well_formed(out)


def test_published_page_with_two_judges(tmp_path):
    out = render(PAGE, _release(tmp_path))
    r = regions(out)
    assert r.count('class="case-card') == 3 and 'Spearman <span class="rho">ρ</span> = 1.00' in r and "on every one of" in r and "cross-checked by a second, different-vendor judge" in r
    assert r.count("bc-bar-a") == 3 and r.count("bc-bar-b") == 3  # two bars per candidate
    assert "Zero violations" in r and "Held-out gap" in r
    assert 'href="https://github.com/gpatwa/multi-agent-eval/blob/main/docs/results/public/report.md"' in r
    assert 'docs/results/heldout/summary.json"' in r and "heldout/report.md" not in r  # held-out: aggregates only
    assert "aria-label=\"Composite score by candidate, scored by two judges." in r
    assert_well_formed(r)


def test_published_page_without_second_judge_makes_no_agreement_claim(tmp_path):
    r = regions(render(PAGE, _release(tmp_path, second=False)))
    assert "Spearman" not in r and "cross-checked" not in r and r.count("bc-bar-b") == 0
    assert "Every run reports how it was judged" in r
    assert_well_formed(r)


def test_violations_are_counted_never_called_zero(tmp_path):
    flagged = Verdict(scores={"routing": 5, "priority": 5, "policy_adherence": 3, "resolution": 3, "tone": 3},
                      overall=3.0, flags=["policy_critical"], rationale="routed a/b vs gold a/b. Promised a refund.")
    public = make_run(tmp_path, "p", CANDS, verdicts={("t1", "gpt"): flagged})
    from eval_agents.publish import build_release
    data = build_release(suites={"public": public}, second=None, walkthrough_task="t3", providers=19)
    r = regions(render(PAGE, data))
    assert "Zero violations" not in r and "1 guardrail flag" in r and "policy_critical" in r and 'dot-warn">1<' in r
    # the scoreboard marks the flagged candidate and says the flags are outside the composite
    assert r.count('class="fl">1 flag<') == 1 and "separate gate, not part of this score" in r


def test_text_from_data_is_escaped(tmp_path):
    data = _release(tmp_path)
    data["walkthrough"]["cards"][0]["reply_excerpt"] = '<script>alert("x")</script> & more'
    r = regions(render(PAGE, data))
    assert "<script>alert" not in r and "&lt;script&gt;" in r and "&amp; more" in r


def test_lower_ranked_best_quality_candidate_is_explained_from_numbers(tmp_path):
    # claude: best quality but slowest -> the note must cite the real latency numbers
    slow = {"claude": 30.0, "gpt": 5.0, "gemini": 6.0}
    hi = Verdict(scores={"routing": 5, "priority": 5, "policy_adherence": 5, "resolution": 5, "tone": 5}, overall=5.0, rationale="routed a/b vs gold a/b. ok")
    public = make_run(tmp_path, "p", CANDS, latency=slow, verdicts={(t, "claude"): hi for t in ("t1", "t2", "t3")})
    from eval_agents.publish import build_release
    r = regions(render(PAGE, build_release(suites={"public": public}, second=None, walkthrough_task="t3", providers=19)))
    assert "claude has the highest raw reply quality" in r and "p95 latency is 30.0s vs 5.0–6.0s" in r


def test_value_labels_that_would_clip_go_inside_the_bar(tmp_path):
    data = _release(tmp_path)
    for i, c in enumerate(data["suites"]["public"]["candidates"].values()):
        c["composite"] = [0.99, 0.80, 0.50][i]
    data["agreement"]["public"]["composite_second_judge"] = {n: 0.4 for n in CANDS}
    r = regions(render(PAGE, data))
    assert r.count("bc-value-in") == 1  # only the 0.99 bar would run off the chart
    assert 'text-anchor="end">0.990<' in r and ">0.800<" in r


README = (ROOT / "README.md").read_text()


def test_committed_readme_is_the_render_of_the_committed_data():
    assert render_readme(README, DATA) == README, "run: python scripts/build_site.py"


def test_readme_states_only_what_the_data_supports(tmp_path):
    pending = render_readme(README, {**DATA, "status": "pending"})
    assert "being refreshed" in pending and "Zero guardrail flags" not in pending and "Spearman" not in pending
    md = render_readme(README, _release(tmp_path))
    assert "| claude |" in md and "Zero guardrail flags" in md and "Spearman ρ = 1.00" in md
    assert "docs/results/public/report.md" in md and "**claude leads on both**" in md


# ---------------------------------------------------------------- one site URL, everywhere


def test_every_generated_file_is_the_render_of_the_committed_data():
    for path, text in generated_files(ROOT, DATA):
        assert path.read_text() == text, f"{path.relative_to(ROOT)} is stale: run python scripts/build_site.py"


def test_committed_data_carries_the_site_url_from_release_yaml():
    import yaml

    site = (yaml.safe_load((ROOT / "release.yaml").read_text()).get("site") or {})["url"].rstrip("/")
    assert DATA["site_url"] == site and site.startswith("https://")


def test_head_urls_follow_the_site_url():
    import json as _json

    out = head({"site_url": "https://eval.aveto.dev/"})  # a trailing slash must not double up
    assert '<link rel="canonical" href="https://eval.aveto.dev/">' in out
    assert 'property="og:url" content="https://eval.aveto.dev/"' in out and 'property="og:image" content="https://eval.aveto.dev/og-image.png"' in out
    assert 'name="twitter:image" content="https://eval.aveto.dev/og-image.png"' in out
    ld = _json.loads(out.split('<script type="application/ld+json">')[1].split("</script>")[0])
    site, software = ld["@graph"]
    assert site["@id"] == "https://eval.aveto.dev/#website" and software["url"] == "https://eval.aveto.dev/"


def test_head_never_names_a_wrong_domain_when_none_is_configured():
    import json as _json

    out = head({})
    assert "canonical" not in out and 'og:url' not in out and 'og:image" content' not in out and 'twitter:image' not in out
    ld = _json.loads(out.split('<script type="application/ld+json">')[1].split("</script>")[0])
    assert "url" not in ld["@graph"][0] and "@id" not in ld["@graph"][0]


def test_sitemap_and_robots_derive_from_the_same_url():
    assert "<loc>https://eval.aveto.dev/</loc>" in sitemap({"site_url": "https://eval.aveto.dev"})
    assert robots({"site_url": "https://eval.aveto.dev"}).endswith("Sitemap: https://eval.aveto.dev/sitemap.xml\n")
    assert "<loc>" not in sitemap({}) and "Sitemap:" not in robots({})


def test_the_static_title_matches_the_one_the_head_repeats_in_og_title():
    assert f"<title>{TITLE}</title>" in PAGE


def test_no_stale_domain_is_left_anywhere_outside_the_tests():
    """The old domain is gone from everything that ships (the site URL lives only in release.yaml)."""
    stale = "aveto" + ".com"
    offenders = []
    for p in list((ROOT / "docs").rglob("*")) + [ROOT / "README.md", ROOT / "release.yaml"] + list((ROOT / "scripts").glob("*.py")) \
            + list((ROOT / ".github").rglob("*.yml")) + list((ROOT / "eval_agents").glob("*.py")) + list((ROOT / "assets").glob("*")):
        if p.is_file() and p.suffix in {".html", ".xml", ".txt", ".md", ".yaml", ".yml", ".py", ".json"} and stale in p.read_text():
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"still mentions {stale}: {offenders}"
