"""Render docs/index.html's data-bearing regions from docs/data/results.json.

    python scripts/build_site.py                  # re-render the page from the data
    python scripts/build_site.py --check          # exit 1 if the committed page isn't the render of the data (CI)
    python scripts/build_site.py --pending "why"  # write "no valid results yet" data (sizes only), then render

The page's prose and design are hand-written; only the AUTO regions are generated,
and only from validated data (see eval_agents/publish.py, eval_agents/site.py).
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.config import load_tasks  # noqa: E402
from eval_agents.publish import pending_data  # noqa: E402
from eval_agents.registry import _PROVIDERS  # noqa: E402
from eval_agents.site import render, render_readme  # noqa: E402

PAGE = ROOT / "docs" / "index.html"
README = ROOT / "README.md"
DATA = ROOT / "docs" / "data" / "results.json"
SUITE_FILES = {"public": "tasks.triage.yaml", "heldout": "tasks.triage.private.yaml"}


def pending_from_tasks(reason: str) -> dict:
    suites = {}
    for name, fname in SUITE_FILES.items():
        path = ROOT / fname
        if path.exists():
            tasks = load_tasks(path)
            suites[name] = {"n_tasks": len(tasks), "n_guardrail": sum(t.category == "guardrail" for t in tasks)}
    return pending_data(suites=suites, providers=len(_PROVIDERS) - 1, reason=reason)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--pending", metavar="REASON")
    args = ap.parse_args(argv)
    if args.pending:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        DATA.write_text(json.dumps(pending_from_tasks(args.pending), indent=2) + "\n")
    data = json.loads(DATA.read_text())
    stale = []
    for path, fn in ((PAGE, render), (README, render_readme)):
        current = path.read_text()
        rendered = fn(current, data)
        if rendered == current:
            continue
        if args.check:
            diff = difflib.unified_diff(current.splitlines(), rendered.splitlines(), "committed", "rendered", lineterm="", n=1)
            stale.append(f"{path.relative_to(ROOT)}\n" + "\n".join(list(diff)[:30]))
        else:
            path.write_text(rendered)
            print(f"updated {path.relative_to(ROOT)} ({data['status']})", file=sys.stderr)
    if stale:
        print("Generated regions are out of date with docs/data/results.json (edited by hand, or data changed).\n"
              "Run: python scripts/build_site.py\n\n" + "\n\n".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
