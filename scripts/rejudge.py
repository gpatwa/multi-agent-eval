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


def rejudge(results: list[TaskResult], judge: Agent, scorer) -> list[TaskResult]:
    total = sum(1 for tr in results for r in tr.results if not r.error)
    done = 0
    for tr in results:
        for r in tr.results:
            if r.error:
                continue
            r.verdict = scorer(judge, tr.task, r.answer)
            done += 1
            shown = r.verdict.parse_error or f"overall {r.verdict.overall}"
            print(f"[{done}/{total}] {tr.task.id} {r.candidate}: {shown}", file=sys.stderr)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", type=pathlib.Path, help="run directory containing results.json")
    ap.add_argument("--config", required=True, help="config whose judge/scorer/scorecard to use")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args(argv)

    config = load_config(args.config)
    _, scorer = select_use_case(config)
    spec = config["judge"]
    judge = Agent(name="judge", provider=create_provider(spec["provider"], spec["model"]), system=JUDGE_SYSTEM)

    results = rejudge(load_results(args.source), judge, scorer)
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
