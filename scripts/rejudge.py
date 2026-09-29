"""Re-score a finished run's stored answers with a different judge.

    python scripts/rejudge.py results-triage-run4 --config config.triage.mixed.gemini-judge.yaml \
        --out results-triage-run4-b
    python scripts/compare_judges.py results-triage-run4/summary.json results-triage-run4-b/summary.json

No candidate is re-run: every answer, latency and token count is copied from
the source run, and only the verdicts are recomputed by the config's judge
(with the config's use-case scorer and scorecard). The judge is then the
only variable between the two runs, so judge agreement measures the judge
and not candidate sampling noise. It's also cheap: judge calls only.

Candidate errors stay errors (nothing to score). Use-cases whose scoring is
fully deterministic re-score identically.

Quota-friendly: subscription and free-tier judges run out mid-run. The script
stops after MAX_CONSECUTIVE_FAILURES judge failures in a row (instead of
burning through every remaining answer), still writes what it has, and exits 3.
Pick up where it left off with --resume, which reuses every successfully judged
verdict from the earlier output and only re-judges the rest:

    python scripts/rejudge.py results-triage-run4 --config config.triage.mixed.codex-judge.yaml \
        --resume results-triage-run4-codexjudge --out results-triage-run4-codexjudge

(--out may be the same directory as --resume.) A verdict is only reused when
its answer is byte-identical to the source run's, so a source that changed
since (e.g. a spliced rerun) is re-judged rather than silently mixed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.config import load_config, select_use_case  # noqa: E402
from eval_agents.judge import JUDGE_SYSTEM  # noqa: E402
from eval_agents.agents import Agent  # noqa: E402
from eval_agents.registry import create_provider  # noqa: E402
from eval_agents.report import JUDGE_FAILURE_WARN, summarize, to_json, to_markdown, to_summary_json  # noqa: E402
from eval_agents.runner import CandidateResult, Task, TaskResult  # noqa: E402


def load_results(path: pathlib.Path) -> list[TaskResult]:
    """Rebuild TaskResult objects from a results.json (verdicts dropped)."""
    data = json.loads((path / "results.json").read_text())
    out = []
    for tr in data:
        task = Task(**tr["task"])
        results = [
            CandidateResult(**{k: v for k, v in r.items() if k != "verdict"})
            for r in tr["results"]
        ]
        out.append(TaskResult(task=task, results=results))
    return out


MAX_CONSECUTIVE_FAILURES = 5


def _key(task_id: str, r: CandidateResult) -> tuple:
    return (task_id, r.candidate, r.trial)


def reusable_verdicts(source: list[TaskResult], partial: list[TaskResult]) -> dict:
    """{(task, candidate, trial): Verdict} for answers a previous re-judge scored
    successfully AND whose answer text still matches the source run."""
    answers = {_key(tr.task.id, r): r.answer for tr in source for r in tr.results}
    reuse = {}
    for tr in partial:
        for r in tr.results:
            k = _key(tr.task.id, r)
            if r.verdict and not r.verdict.parse_error and answers.get(k) == r.answer:
                reuse[k] = r.verdict
    return reuse


def load_results_with_verdicts(path: pathlib.Path) -> list[TaskResult]:
    """Like load_results, but keeps each stored verdict."""
    from eval_agents.judge import Verdict

    data = json.loads((path / "results.json").read_text())
    out = load_results(path)
    for tr, raw in zip(out, data):
        for r, rr in zip(tr.results, raw["results"]):
            if rr.get("verdict"):
                r.verdict = Verdict(**rr["verdict"])
    return out


def rejudge(results: list[TaskResult], judge: Agent, scorer, reuse: dict | None = None,
            max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES) -> tuple[list[TaskResult], dict]:
    """Score every non-error answer. Returns (results, stats) with counts of
    reused / judged / failed verdicts and whether the run was aborted."""
    from eval_agents.judge import Verdict

    reuse = reuse or {}
    stats = {"reused": 0, "judged": 0, "failed": 0, "aborted": False}
    total = sum(1 for tr in results for r in tr.results if not r.error)
    seen = streak = 0
    for tr in results:
        for r in tr.results:
            if r.error:
                continue
            seen += 1
            k = _key(tr.task.id, r)
            if k in reuse:
                r.verdict = reuse[k]
                stats["reused"] += 1
                continue
            if stats["aborted"]:
                r.verdict = Verdict(parse_error="judge: not attempted (aborted after repeated judge failures)")
                stats["failed"] += 1
                continue
            r.verdict = scorer(judge, tr.task, r.answer)
            if r.verdict.parse_error:
                stats["failed"] += 1
                streak += 1
                if streak >= max_consecutive_failures:
                    stats["aborted"] = True
                    print(f"aborting: {streak} judge failures in a row — likely quota or credentials; "
                          "resume later with --resume", file=sys.stderr)
            else:
                stats["judged"] += 1
                streak = 0
            shown = r.verdict.parse_error or f"overall {r.verdict.overall}"
            print(f"[{seen}/{total}] {tr.task.id} {r.candidate}: {shown[:120]}", file=sys.stderr)
    return results, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", type=pathlib.Path, help="run directory containing results.json")
    ap.add_argument("--config", required=True, help="config whose judge/scorer/scorecard to use")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--resume", type=pathlib.Path, metavar="DIR",
                    help="earlier (partial) re-judge output: reuse its successful verdicts, judge only the rest")
    ap.add_argument("--max-consecutive-failures", type=int, default=MAX_CONSECUTIVE_FAILURES,
                    help="stop after this many judge failures in a row (default: %(default)s)")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    _, scorer = select_use_case(config)
    spec = config["judge"]
    judge = Agent(name="judge", provider=create_provider(spec["provider"], spec["model"]), system=JUDGE_SYSTEM)

    results = load_results(args.source)
    reuse = {}
    if args.resume:
        reuse = reusable_verdicts(results, load_results_with_verdicts(args.resume))
        print(f"resuming: reusing {len(reuse)} verdicts from {args.resume}", file=sys.stderr)
    results, stats = rejudge(results, judge, scorer, reuse, args.max_consecutive_failures)
    print(f"verdicts: {stats['reused']} reused, {stats['judged']} newly judged, {stats['failed']} failed",
          file=sys.stderr)
    args.out.mkdir(parents=True, exist_ok=True)
    scorecard = config.get("scorecard")
    (args.out / "results.json").write_text(to_json(results))
    (args.out / "summary.json").write_text(to_summary_json(results, scorecard=scorecard))
    (args.out / "report.md").write_text(to_markdown(results, scorecard=scorecard))
    (args.out / "rejudged_from.txt").write_text(f"{args.source}\njudge: {spec['provider']}/{spec['model']}\n")
    print(f"Re-judged report written to {args.out / 'report.md'}", file=sys.stderr)
    rates = {n: s["judge_failure_rate"] for n, s in summarize(results, scorecard)["candidates"].items()}
    bad = {n: r for n, r in rates.items() if r > JUDGE_FAILURE_WARN}
    if bad:
        print("ERROR: judge failed on " + ", ".join(f"{n} {r:.0%}" for n, r in bad.items())
              + f" of verdicts (> {JUDGE_FAILURE_WARN:.0%}); this re-judge is not usable for comparison "
              "(check the judge's quota/credentials and re-run)", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
