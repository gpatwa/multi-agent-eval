"""The release pipeline against a fake command runner: quota stops, resume, gating, idempotence."""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest
import yaml

from eval_agents.judge import Verdict
from eval_agents.pipeline import EXIT_BLOCKED, EXIT_OK, EXIT_WAITING, Pipeline, Release, default_exec
from eval_agents.report import to_json, to_markdown, to_summary_json
from eval_agents.results_io import load_results, load_settings
from tests.test_publish import CANDS, JUDGE_A, JUDGE_B, make_run

REPO = pathlib.Path(__file__).resolve().parent.parent
PUBLIC_TASKS = [("t1", "ticket"), ("t2", "ticket"), ("t3", "guardrail")]
HELD_TASKS = [("heldout-ticket-1", "ticket"), ("heldout-ticket-2", "guardrail")]
SUITE_TASKS = {"public": PUBLIC_TASKS, "heldout": HELD_TASKS}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeExec:
    """Stands in for main.py and rejudge.py (models/CLIs); merge_runs.py runs for real."""

    def __init__(self, quota_run=(), second_quota_first=False, second_judge=JUDGE_B):
        # A usage limit belongs to the provider, not the suite: the first call for each named candidate
        # (whichever suite it's for) hits it, and the limit is over by the next attempt.
        self.calls, self.quota_left, self.second_quota_first = [], {c: 1 for c in quota_run}, second_quota_first
        self.second_judge, self.rejudge_calls = second_judge, 0

    def __call__(self, cmd, env, cwd):
        script, args = pathlib.Path(cmd[1]).name, cmd[2:]
        self.calls.append((script, args))
        opt = lambda flag: args[args.index(flag) + 1] if flag in args else None  # noqa: E731
        if script == "merge_runs.py":
            return default_exec(cmd, env, cwd)
        if script == "main.py":
            suite = "public" if opt("--tasks").endswith("tasks.triage.yaml") else "heldout"
            cands = opt("--only").split(",")
            hit = {c for c in cands if self.quota_left.get(c, 0) > 0}
            for c in hit:
                self.quota_left[c] -= 1
            errors = {(t, c) for t, _ in SUITE_TASKS[suite] for c in hit}
            out = pathlib.Path(opt("--out"))
            out.parent.mkdir(parents=True, exist_ok=True)
            make_run(out.parent, out.name, cands, tasks=SUITE_TASKS[suite], errors=errors)
            return (1, "ERROR: You've hit your usage limit") if errors else (0, "")
        if script == "rejudge.py":
            self.rejudge_calls += 1
            second = "codex" in opt("--config")
            return self._rejudge(pathlib.Path(args[0]), pathlib.Path(opt("--out")), self.second_judge if second else JUDGE_A,
                                 fail=second and self.second_quota_first and self.rejudge_calls == 1, resume=opt("--resume"))
        raise AssertionError(f"unexpected {script}")

    @staticmethod
    def _rejudge(source, out, judge, fail, resume):
        results = load_results(source)
        prior = {(tr.task.id, r.candidate): r.verdict for tr in load_results(pathlib.Path(resume), with_verdicts=True) for r in tr.results} if resume else {}
        n = 0
        for tr in results:
            for r in tr.results:
                n += 1
                good = prior.get((tr.task.id, r.candidate))
                if good and not good.parse_error:
                    r.verdict = good
                elif fail and n > 3:
                    r.verdict = Verdict(parse_error="judge: usage limit")
                else:
                    r.verdict = Verdict(scores={"routing": 5, "priority": 5, "policy_adherence": 5, "resolution": 4, "tone": 5}, overall=4.9)
        settings = {**load_settings(source), "judge": judge}
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(to_json(results))
        (out / "summary.json").write_text(to_summary_json(results, {"weights": {"quality": 0.7, "latency": 0.3}}, settings=settings))
        (out / "report.md").write_text(to_markdown(results, settings=settings))
        return (3, "usage limit") if fail else (0, "")


class FakeGit:
    def __init__(self):
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        return (1, "") if args[:2] == ["diff", "--cached"] else (0, "")

    def commits(self):
        return [c for c in self.calls if c[0] == "commit"]


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "data").mkdir(parents=True)
    shutil.copy(REPO / "docs" / "index.html", root / "docs" / "index.html")
    # start from a pending stub, never the repo's live data (which becomes "published")
    (root / "docs" / "data" / "results.json").write_text(json.dumps({
        "schema": 1, "status": "pending", "reason": "test", "providers": 19, "site_url": "https://eval.aveto.dev",
        "suites": {"public": {"n_tasks": 40, "n_guardrail": 7}, "heldout": {"n_tasks": 20, "n_guardrail": 5}}}))
    shutil.copy(REPO / "README.md", root / "README.md")
    for cfg in ("config.triage.mixed.yaml", "config.triage.mixed.codex-judge.yaml"):
        shutil.copy(REPO / cfg, root / cfg)
    (root / "tasks.triage.private.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": tid, "category": cat, "prompt": f"Subject: {tid} subject\nBody text for {tid} that is long enough to matter.", "gold": {}}
        for tid, cat in HELD_TASKS]}))
    release = Release(name="test", config="config.triage.mixed.yaml", second_judge_config="config.triage.mixed.codex-judge.yaml",
                      require_second_judge=True, walkthrough_task="t3", min_judge_coverage=0.9,
                      suites={"public": {"tasks": "tasks.triage.yaml"}, "heldout": {"tasks": "tasks.triage.private.yaml", "private": True}},
                      candidates=CANDS, env_unset=["ANTHROPIC_AUTH_TOKEN"], commit_trailer="Co-Authored-By: Test <t@example.com>",
                      site_url="https://eval.aveto.dev")

    class W:
        pass

    w = W()
    w.root, w.release, w.clock, w.git = root, release, Clock(), FakeGit()

    def pipeline(fake):
        return Pipeline(release, root, exec_fn=fake, git_fn=w.git, now=w.clock, log=lambda m: None)

    def seed_valid(p, candidates=("claude", "gemini")):
        for suite in SUITE_TASKS:
            d = make_run(tmp_path, f"seed-{suite}", list(candidates), tasks=SUITE_TASKS[suite])
            p.adopt(suite, d)

    w.pipeline, w.seed_valid, w.tmp = pipeline, seed_valid, tmp_path
    return w


def test_runs_only_the_missing_candidate_then_publishes_everything(world):
    fake = FakeExec()
    p = world.pipeline(fake)
    world.seed_valid(p)
    result = p.tick(publish=True)
    assert result.state == "published" and result.exit_code == EXIT_OK, result.problems
    assert [(s, a[a.index("--only") + 1]) for s, a in fake.calls if s == "main.py"] == [("main.py", "gpt"), ("main.py", "gpt")]
    data = json.loads((world.root / "docs" / "data" / "results.json").read_text())
    assert data["status"] == "published" and set(data["suites"]["public"]["candidates"]) == set(CANDS)
    assert data["agreement"]["public"]["judge_second"]["provider"] == "CodexProvider"
    assert (world.root / "docs" / "results" / "public" / "report.md").exists()
    assert (world.root / "docs" / "results" / "heldout" / "summary.json").exists()
    assert not (world.root / "docs" / "results" / "heldout" / "report.md").exists()  # report.md quotes ticket text
    page = (world.root / "docs" / "index.html").read_text()
    assert "being refreshed" not in page and "Spearman" in page
    assert "Spearman ρ" in (world.root / "README.md").read_text() and "being refreshed" not in (world.root / "README.md").read_text()
    assert ["add", "docs", "README.md"] in world.git.calls
    # every URL the page names comes from release.yaml's site.url, written by the same publish
    assert "<loc>https://eval.aveto.dev/</loc>" in (world.root / "docs" / "sitemap.xml").read_text()
    assert "Sitemap: https://eval.aveto.dev/sitemap.xml" in (world.root / "docs" / "robots.txt").read_text()
    assert '<link rel="canonical" href="https://eval.aveto.dev/">' in page
    assert json.loads((world.root / "docs" / "data" / "results.json").read_text())["site_url"] == "https://eval.aveto.dev"
    published_text = "".join(f.read_text() for f in (world.root / "docs").rglob("*") if f.is_file())
    assert "Body text for heldout-ticket-1" not in published_text and "heldout-ticket-1" not in published_text
    assert len(world.git.commits()) == 1 and "Co-Authored-By: Test" in world.git.commits()[0][-1]
    assert ["push", "-q", "origin", "HEAD:main"] in world.git.calls


def test_the_real_release_yaml_names_the_site():
    release = Release.load(REPO / "release.yaml")
    assert release.site_url == "https://eval.aveto.dev"


def test_quota_stop_sets_a_cooldown_returns_quickly_and_retries_later(world):
    fake = FakeExec(quota_run={"gpt"})
    p = world.pipeline(fake)
    world.seed_valid(p)
    first = p.tick()
    assert first.state == "waiting" and first.exit_code == EXIT_WAITING and any(w.startswith("run:gpt") for w in first.waiting_on)
    n_runs = sum(1 for s, _ in fake.calls if s == "main.py")
    assert n_runs == 1  # the usage limit on the public suite paused gpt everywhere: held-out wasn't attempted
    # still inside the cooldown: no candidate call is made at all
    again = world.pipeline(fake).tick()
    assert again.state == "waiting" and sum(1 for s, _ in fake.calls if s == "main.py") == n_runs
    # after the cooldown the retry succeeds and the release goes out
    world.clock.t += 21 * 60
    done = world.pipeline(fake).tick(publish=True)
    assert done.state == "published", done.problems


def test_second_judge_resumes_after_a_quota_stop_instead_of_restarting(world):
    fake = FakeExec(second_quota_first=True)
    p = world.pipeline(fake)
    world.seed_valid(p)
    first = p.tick()
    assert not any(a[0].endswith("merged/heldout") for s, a in fake.calls if s == "rejudge.py" and "codex" in a[a.index("--config") + 1])
    assert first.state == "waiting" and any(w.startswith("judge:second") for w in first.waiting_on)
    world.clock.t += 21 * 60
    done = world.pipeline(fake).tick(publish=True)
    assert done.state == "published", done.problems
    public = [a for s, a in fake.calls if s == "rejudge.py" and "codex" in a[a.index("--config") + 1] and a[0].endswith("merged/public")]
    assert "--resume" not in public[0] and "--resume" in public[1]  # the retry reused what was judged
    heldout_second = [a for s, a in fake.calls if s == "rejudge.py" and "codex" in a[a.index("--config") + 1] and a[0].endswith("merged/heldout")]
    assert len(heldout_second) == 1  # the quota stop paused the held-out suite too: it ran once, after the cooldown


def test_gate_refuses_a_second_judge_from_the_same_vendor(world):
    fake = FakeExec(second_judge=JUDGE_A)  # the "second" judge is the same vendor as the first
    p = world.pipeline(fake)
    world.seed_valid(p)
    result = p.tick(publish=True)
    assert result.state == "blocked" and result.exit_code == EXIT_BLOCKED and any("same vendor" in x for x in result.problems)
    assert json.loads((world.root / "docs" / "data" / "results.json").read_text())["status"] == "pending"
    assert world.git.commits() == []


def test_republishing_unchanged_results_makes_no_commit(world):
    fake = FakeExec()
    p = world.pipeline(fake)
    world.seed_valid(p)
    assert p.tick(publish=True).state == "published"
    again = world.pipeline(fake).tick(publish=True)
    assert again.state == "current" and len(world.git.commits()) == 1


def test_runs_recorded_without_settings_are_rerun_not_trusted(world):
    fake = FakeExec()
    p = world.pipeline(fake)
    world.seed_valid(p)
    (pathlib.Path(p.state["runs"]["public"][0]) / "summary.json").write_text(
        json.dumps({k: v for k, v in json.loads((pathlib.Path(p.state["runs"]["public"][0]) / "summary.json").read_text()).items() if k != "run_settings"}))
    p.tick()
    only = [a[a.index("--only") + 1] for s, a in fake.calls if s == "main.py" and "tasks.triage.yaml" in " ".join(a)]
    assert set(",".join(only).split(",")) == {"claude", "gpt", "gemini"}  # every candidate of that run is redone


def test_held_out_text_in_a_public_artifact_is_refused(world):
    fake = FakeExec()
    p = world.pipeline(fake)
    world.seed_valid(p)
    for suite in SUITE_TASKS:  # build the merged results without publishing anything
        assert p.ensure_runs(suite) and p.ensure_merged(suite)
    merged = p.merged_dir("heldout") / "summary.json"
    s = json.loads(merged.read_text())
    s["note"] = "Body text for heldout-ticket-1 that is long enough to matter."
    merged.write_text(json.dumps(s))
    result = world.pipeline(fake).tick(publish=True)
    assert result.state == "blocked" and any("held-out ticket text" in x for x in result.problems)
    assert not (world.root / "docs" / "results" / "heldout").exists()
    assert world.git.commits() == []


def test_health_distinguishes_quota_from_unjudged_from_healthy(world, tmp_path):
    p = world.pipeline(FakeExec())
    quota = make_run(tmp_path, "q", ["gpt"], tasks=PUBLIC_TASKS, errors={("t1", "gpt")})
    (quota / "results.json").write_text((quota / "results.json").read_text().replace('"boom"', '"usage limit reached"'))
    assert p.health(str(quota), "gpt")["why"] == "quota"
    bad = {(t, "gpt"): Verdict(parse_error="judge: x") for t, _ in PUBLIC_TASKS[:2]}
    assert p.health(str(make_run(tmp_path, "u", ["gpt"], tasks=PUBLIC_TASKS, verdicts=bad)), "gpt")["why"] == "unjudged"
    ok = str(make_run(tmp_path, "ok", ["gpt"], tasks=PUBLIC_TASKS))
    assert p.health(ok, "gpt")["ok"] is True and p.health(ok, "missing") is None
