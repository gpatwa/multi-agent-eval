"""The results release pipeline: run -> merge -> second judge -> gate -> publish.

One idempotent, re-entrant `tick()`. Each call looks at what exists, does whatever is
missing, and stops when the next step is blocked. Nothing sleeps: when a subscription hits
a usage limit the tick records a cooldown and returns, and whatever schedules the tick
retries later. That makes the pipeline crash-safe (state is files on disk), cheap to
re-run, and tolerant of the failure that dominated this project: quotas running out
mid-run and quietly degrading results.

Stages, per suite (public, held-out):
  1. run     every required candidate must have a run that is complete: no candidate
             errors and enough judged verdicts. Missing/broken candidates are (re)run
             alone (`main.py --only`); unjudged verdicts are filled by `rejudge --resume`.
  2. merge   best complete run per candidate -> one scorecard (`merge_runs.py`).
  3. judge2  the merged answers re-scored by a second, different-vendor judge
             (`rejudge.py --resume`, so a quota stop loses nothing).
  4. gate    eval_agents.publish.check_gates — only valid results are published.
  5. publish docs/data/results.json + sanitized artifacts + re-rendered page, then
             (optionally) commit and push. CI deploys on push.

Exit codes: 0 published/up to date, 75 waiting (transient: quota/cooldown), 2 blocked by the
gate (needs a look), 1 error.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from .config import load_config, load_tasks, select_effort
from .history import append as append_history, dump as dump_history, load as load_history, regressions
from .publish import build_release, check_gates, leak_probes
from .results_io import load_results, load_settings
from .site import generated_files

REPO = pathlib.Path(__file__).resolve().parent.parent  # where main.py and scripts/ live (may differ from `root` in tests)
EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_WAITING = 0, 1, 2, 75
QUOTA = re.compile(r"usage limit|rate.?limit|\b429\b|RESOURCE_EXHAUSTED|quota|too many requests|overloaded", re.I)
COOLDOWN_S = 20 * 60
REJUDGE_UNUSABLE = 3  # scripts/rejudge.py's exit code when its judge kept failing (quota, credentials)


@dataclass
class Release:
    name: str
    config: str
    second_judge_config: str | None
    require_second_judge: bool
    walkthrough_task: str
    min_judge_coverage: float
    suites: dict  # name -> {"tasks": path, "private": bool}
    candidates: list[str]
    env_unset: list[str] = field(default_factory=list)
    commit_trailer: str = ""
    site_url: str = ""  # where the page is served (release.yaml site.url); the source of every URL the page names
    specs: dict = field(default_factory=dict)  # candidate name -> {"model", "effort"} the config asks for now

    @classmethod
    def load(cls, path: pathlib.Path) -> "Release":
        d = yaml.safe_load(pathlib.Path(path).read_text())
        cfg = load_config(pathlib.Path(path).parent / d["config"])
        specs = {c["name"]: {"model": c["model"], "effort": select_effort(cfg, c)} for c in cfg["candidates"]}
        return cls(
            name=d["name"], config=d["config"], second_judge_config=d.get("second_judge_config"),
            require_second_judge=bool(d.get("require_second_judge", False)), walkthrough_task=d["walkthrough_task"],
            min_judge_coverage=float(d.get("min_judge_coverage", 0.9)), suites=d["suites"],
            candidates=[c["name"] for c in cfg["candidates"]], env_unset=d.get("env_unset", []),
            commit_trailer=d.get("commit_trailer", ""), site_url=((d.get("site") or {}).get("url") or "").rstrip("/"),
            specs=specs,
        )


@dataclass
class Result:
    state: str = "waiting"  # published | current | waiting | blocked | error
    actions: list[str] = field(default_factory=list)
    waiting_on: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {"published": EXIT_OK, "current": EXIT_OK, "waiting": EXIT_WAITING, "blocked": EXIT_BLOCKED}.get(self.state, EXIT_ERROR)


def default_exec(cmd: list[str], env: dict, cwd: pathlib.Path) -> tuple[int, str]:
    """Run a command, returning (exit code, stderr tail). stdin is closed (no CLI may wait on it)."""
    p = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode, (p.stderr or "")[-3000:]


def default_git(args: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout + p.stderr).strip()


class Pipeline:
    def __init__(self, release: Release, root: pathlib.Path, *, home: pathlib.Path | None = None,
                 exec_fn=default_exec, git_fn=default_git, now=time.time, log=lambda m: print(m, file=sys.stderr)):
        self.r, self.root = release, pathlib.Path(root)
        self.home = pathlib.Path(home) if home else self.root / ".pipeline"
        self.exec, self.git, self.now, self.log = exec_fn, git_fn, now, log
        self.state_path = self.home / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"runs": {}, "cooldown": {}}
        self.result = Result()

    # -------------------------------------------------------------- plumbing

    def _save(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in set(self.r.env_unset)}
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _run(self, label: str, cmd: list[str]) -> tuple[int, str]:
        self.log(f"[pipeline] {label}: {' '.join(cmd[1:])}")
        self.result.actions.append(label)
        return self.exec([sys.executable, str(REPO / cmd[0]), *cmd[1:]], self._env(), self.root)

    # Cooldown keys are per PROVIDER quota, never per suite: a usage limit hit on one suite
    # will hit the next suite too, so a stop must pause every stage that uses that provider.
    #   run:<candidate>   judge:primary   judge:second
    def cooling(self, key: str) -> bool:
        return self.now() < self.state["cooldown"].get(key, 0)

    def cooldown(self, key: str, why: str):
        self.state["cooldown"][key] = self.now() + COOLDOWN_S
        self.result.waiting_on.append(f"{key} ({why}; retry in {COOLDOWN_S // 60} min)")
        self._save()

    def adopt(self, suite: str, run_dir: pathlib.Path):
        runs = self.state["runs"].setdefault(suite, [])
        if str(run_dir) not in runs:
            runs.append(str(run_dir))
            self._save()

    # -------------------------------------------------------------- run health

    def health(self, run_dir: str, candidate: str) -> dict | None:
        """{'ok', 'why'} for one candidate in one run; None if the run lacks it."""
        d = pathlib.Path(run_dir)
        try:
            summary = json.loads((d / "summary.json").read_text())
            raw = json.loads((d / "results.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None
        c = summary["candidates"].get(candidate)
        if c is None:
            return None
        errors = [r["error"] for tr in raw for r in tr["results"] if r["candidate"] == candidate and r.get("error")]
        if errors:
            return {"ok": False, "why": "quota" if any(QUOTA.search(e) for e in errors) else "error", "detail": errors[0][:160]}
        if c.get("judge_failure_rate", 0.0) > 1 - self.r.min_judge_coverage:
            return {"ok": False, "why": "unjudged", "detail": f"{c['judge_failure_rate']:.0%} of verdicts unjudged"}
        ran = (load_settings(d) or {}).get("candidates", {}).get(candidate)
        if not ran:
            return {"ok": False, "why": "no-settings", "detail": "run predates settings recording"}
        # A run only counts for what the config asks for now: bump a model ID or the effort and the old run is stale.
        want = self.r.specs.get(candidate)
        if want and ran.get("model") != want["model"]:
            return {"ok": False, "why": "stale-model", "detail": f"run used {ran.get('model')}, config asks for {want['model']}"}
        if want and want["effort"] and ran.get("effort") != want["effort"]:
            return {"ok": False, "why": "stale-effort", "detail": f"run used effort {ran.get('effort')}, config asks for {want['effort']}"}
        return {"ok": True, "why": "", "detail": ""}

    def plan(self, suite: str) -> tuple[dict, dict]:
        """(best valid run dir per candidate or None, health of the newest run per candidate)."""
        best, latest = {}, {}
        for c in self.r.candidates:
            best[c], latest[c] = None, None
            for run in reversed(self.state["runs"].get(suite, [])):  # newest first
                h = self.health(run, c)
                if h is None:
                    continue
                latest[c] = latest[c] or (run, h)
                if h["ok"]:
                    best[c] = run
                    break
        return best, latest

    # -------------------------------------------------------------- stages

    def ensure_runs(self, suite: str) -> bool:
        """Make every required candidate valid for `suite`. True when they all are."""
        spec = self.r.suites[suite]
        best, latest = self.plan(suite)
        # 1) candidates whose answers are fine but whose verdicts aren't fully judged: fill, don't re-run
        for c, entry in list(latest.items()):
            if best[c] is not None or not (entry and entry[1]["why"] == "unjudged"):
                continue
            if self.cooling("judge:primary"):
                self.result.waiting_on.append("judge:primary (cooling down after a quota stop)")
            else:
                run = entry[0]
                out = f"{run.rstrip('/')}-filled"
                code, err = self._run(f"fill judge for {suite}/{c}", ["scripts/rejudge.py", run, "--config", self.r.config,
                                                                      "--resume", run, "--out", out])
                self.adopt(suite, pathlib.Path(out))
                if code == REJUDGE_UNUSABLE or (code != 0 and QUOTA.search(err)):
                    self.cooldown("judge:primary", "judge quota")
                best, latest = self.plan(suite)
        # 2) candidates with no valid run: run them (all in one go)
        missing = [c for c in self.r.candidates if best[c] is None and not (latest[c] and latest[c][1]["why"] == "unjudged")]
        runnable = [c for c in missing if not self.cooling(f"run:{c}")]
        for c in set(missing) - set(runnable):
            self.result.waiting_on.append(f"run:{c} (cooling down after a quota stop)")
        if runnable:
            out = self.home / "runs" / suite / f"{'-'.join(runnable)}-{int(self.now())}"
            code, err = self._run(f"run {suite}/{','.join(runnable)}", [
                "main.py", "--config", self.r.config, "--tasks", spec["tasks"], "--only", ",".join(runnable), "--out", str(out)])
            if (out / "results.json").exists():
                self.adopt(suite, out)
            best, latest = self.plan(suite)
            for c in runnable:
                if best[c] is None:
                    h = (latest[c] or (None, {"why": "error", "detail": err[-160:]}))[1]
                    if h["why"] == "quota" or (code != 0 and QUOTA.search(err)):
                        self.cooldown(f"run:{c}", "usage limit")
                    elif h["why"] != "unjudged":
                        self.result.problems.append(f"{suite}/{c}: {h['why']}: {h['detail']}")
        return all(best[c] for c in self.r.candidates)

    def merged_dir(self, suite: str) -> pathlib.Path:
        return self.home / "merged" / suite

    def ensure_merged(self, suite: str) -> bool:
        best, _ = self.plan(suite)
        by_run: dict[str, list[str]] = {}
        for c in self.r.candidates:
            by_run.setdefault(best[c], []).append(c)
        specs = [f"{run}:{','.join(cs)}" for run, cs in by_run.items()]
        out = self.merged_dir(suite)
        stamp = out / "merged_from.txt"
        if stamp.exists() and stamp.read_text().split() == specs and (out / "summary.json").exists():
            return True
        if len(specs) == 1:  # one run already holds every candidate: still normalise through merge_runs? no — reuse as is
            shutil.rmtree(out, ignore_errors=True)
            shutil.copytree(specs[0].split(":")[0], out)
            stamp.write_text("\n".join(specs) + "\n")
            return True
        code, err = self._run(f"merge {suite}", ["scripts/merge_runs.py", *specs, "--config", self.r.config, "--out", str(out)])
        if code != 0:
            self.result.problems.append(f"{suite}: merge failed: {err[-200:]}")
            return False
        return True

    def second_dir(self, suite: str) -> pathlib.Path:
        return self.home / "second" / suite

    def second_complete(self, suite: str) -> bool:
        d = self.second_dir(suite)
        try:
            s = json.loads((d / "summary.json").read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return bool(s["candidates"]) and all(c.get("judge_failure_rate", 1) <= 1 - self.r.min_judge_coverage for c in s["candidates"].values())

    def ensure_second_judge(self, suite: str) -> bool:
        if not self.r.second_judge_config:
            return True
        merged, out = self.merged_dir(suite), self.second_dir(suite)
        # Redo when the merged answers changed since the second judge saw them.
        src_stamp = out / "rejudged_from_stamp.txt"
        merged_sig = (merged / "merged_from.txt").read_text() if (merged / "merged_from.txt").exists() else ""
        if src_stamp.exists() and src_stamp.read_text() != merged_sig:
            shutil.rmtree(out, ignore_errors=True)
        if self.second_complete(suite):
            return True
        if self.cooling("judge:second"):
            self.result.waiting_on.append("judge:second (cooling down after a quota stop)")
            return False
        cmd = ["scripts/rejudge.py", str(merged), "--config", self.r.second_judge_config, "--out", str(out)]
        if (out / "results.json").exists():
            cmd += ["--resume", str(out)]
        code, err = self._run(f"second judge {suite}", cmd)
        out.mkdir(parents=True, exist_ok=True)
        src_stamp.write_text(merged_sig)
        if self.second_complete(suite):
            return True
        if code == REJUDGE_UNUSABLE or QUOTA.search(err):  # rejudge exits 3 when the judge kept failing (quota/credentials)
            self.cooldown("judge:second", "second judge could not finish")
        else:
            self.result.problems.append(f"{suite}: second judge failed: {err[-200:]}")
        return False

    # -------------------------------------------------------------- publish

    def private_probes(self) -> list[str]:
        probes = []
        for spec in self.r.suites.values():
            if spec.get("private") and (self.root / spec["tasks"]).exists():
                probes += leak_probes(load_tasks(self.root / spec["tasks"]))
        return probes

    def publish(self, do_commit: bool) -> bool:
        from .registry import _PROVIDERS

        names = list(self.r.suites)
        second = {s: self.second_dir(s) for s in names if self.r.second_judge_config and self.second_complete(s)}
        data = build_release(suites={s: self.merged_dir(s) for s in names}, second=second or None,
                             walkthrough_task=self.r.walkthrough_task, providers=len(_PROVIDERS) - 1, suite_order=names,
                             site_url=self.r.site_url)
        probes = self.private_probes()
        problems = check_gates(data, required_candidates=self.r.candidates, require_second_judge=self.r.require_second_judge,
                               private_probes=probes, min_coverage=self.r.min_judge_coverage)
        if problems:
            self.result.problems += problems
            return False

        docs = self.root / "docs"
        artifacts = {}
        for s in names:
            spec, merged = self.r.suites[s], self.merged_dir(s)
            # Held-out suites publish aggregates only: report.md quotes ticket text.
            for fname in (["summary.json"] if spec.get("private") else ["summary.json", "report.md"]):
                artifacts[docs / "results" / s / fname] = (merged / fname).read_text()
        leaked = [p for p in probes if any(json.dumps(p, ensure_ascii=False)[1:-1] in t or p in t for t in artifacts.values())]
        if leaked:
            self.result.problems.append("held-out ticket text found in a public artifact; refusing to publish")
            return False

        data_path = docs / "data" / "results.json"
        strip = lambda d: {k: v for k, v in d.items() if k != "generated"}  # noqa: E731
        prior = json.loads(data_path.read_text()) if data_path.exists() else {}
        history_path = docs / "data" / "history.json"
        history, entry = append_history(load_history(history_path), data)
        generated = generated_files(self.root, data, history=history)  # page, README, sitemap.xml, robots.txt
        generated.append((history_path, dump_history(history)))
        stale_text = any(not p.exists() or p.read_text() != t for p, t in generated)
        changed = strip(prior) != strip(data) or stale_text or any(not p.exists() or p.read_text() != t for p, t in artifacts.items())
        if not changed:
            self.result.state = "current"
            return True
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_text(json.dumps(data, indent=2) + "\n")
        for p, text in [*artifacts.items(), *generated]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.result.actions.append("published data + history + page + README + sitemap")
        for c in regressions(entry["changes"] if entry else []):
            self.result.actions.append(f"regression vs previous published run: {c['suite']}/{c['candidate']} {c['kind']}")
        self.result.state = "published"
        if do_commit:
            self.commit(["docs", "README.md"], f"Publish {self.r.name} results (validated, effort-matched)")
        return True

    def commit(self, paths: list[str], message: str):
        self.git(["add", *paths], self.root)
        if self.git(["diff", "--cached", "--quiet"], self.root)[0] == 0:
            return
        body = f"{message}\n\nGenerated and gated by scripts/pipeline.py; see docs/data/results.json.\n"
        if self.r.commit_trailer:
            body += f"\n{self.r.commit_trailer}\n"
        code, out = self.git(["commit", "-q", "-m", body], self.root)
        if code != 0:
            self.result.problems.append(f"commit failed: {out[-200:]}")
            return
        for attempt in range(2):
            code, out = self.git(["push", "-q", "origin", "HEAD"], self.root)
            if code == 0:
                break
            self.git(["pull", "-q", "--rebase", "origin", "main"], self.root)
        else:
            self.result.problems.append(f"push failed: {out[-200:]}")
            return
        self.git(["fetch", "-q", "origin", "main"], self.root)
        if self.git(["merge-base", "--is-ancestor", "origin/main", "HEAD"], self.root)[0] == 0:
            code, out = self.git(["push", "-q", "origin", "HEAD:main"], self.root)
            if code != 0:
                self.result.problems.append(f"push to main failed: {out[-200:]}")
        else:
            self.result.problems.append("main moved and isn't an ancestor of HEAD; pushed the branch only")

    # -------------------------------------------------------------- tick

    def tick(self, publish: bool = False) -> Result:
        try:
            ready = True
            for suite in self.r.suites:
                ready &= self.ensure_runs(suite)
            for suite in self.r.suites:
                if ready:
                    ready &= self.ensure_merged(suite)
            if ready:
                for suite in self.r.suites:
                    ready &= self.ensure_second_judge(suite)
            if ready:
                published = self.publish(publish)
                if not published:
                    self.result.state = "blocked"
                elif self.result.state == "waiting":
                    self.result.state = "current"
            elif self.result.waiting_on:
                self.result.state = "waiting"
            else:
                self.result.state = "blocked" if self.result.problems else "waiting"
        except Exception as exc:  # never leave the scheduler with a stack trace and no status
            self.result.state = "error"
            self.result.problems.append(f"{type(exc).__name__}: {exc}")
        self.write_status()
        return self.result

    def write_status(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "status.json").write_text(json.dumps({
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "state": self.result.state,
            "waiting_on": self.result.waiting_on, "problems": self.result.problems, "actions": self.result.actions,
            "retry_after_epoch": max(self.state["cooldown"].values(), default=0),
        }, indent=2))
