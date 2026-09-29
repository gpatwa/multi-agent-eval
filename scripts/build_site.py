"""Render everything generated from docs/data/results.json and release.yaml's site URL.

    python scripts/build_site.py                  # re-render the page, README results, sitemap.xml and robots.txt
    python scripts/build_site.py --check          # exit 1 if any committed file isn't the render of the data (CI)
    python scripts/build_site.py --pending "why"  # write "no valid results yet" data (sizes only), then render

The page's prose and design are hand-written; only the AUTO regions are generated, and only from validated data
(see eval_agents/publish.py, eval_agents/site.py). The site URL is config, not a result: it is refreshed from
release.yaml on every run, so moving the site to another domain is a one-line change there.
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.config import load_tasks  # noqa: E402
from eval_agents.publish import pending_data  # noqa: E402
from eval_agents.registry import _PROVIDERS  # noqa: E402
from eval_agents.site import generated_files  # noqa: E402

DOCS = ROOT / "docs"
PAGE = DOCS / "index.html"
README = ROOT / "README.md"
DATA = DOCS / "data" / "results.json"
SUITE_FILES = {"public": "tasks.triage.yaml", "heldout": "tasks.triage.private.yaml"}


def site_url() -> str:
    """Where the site is served, from release.yaml (`site.url`)."""
    release = yaml.safe_load((ROOT / "release.yaml").read_text()) or {}
    return ((release.get("site") or {}).get("url") or "").rstrip("/")


def pending_from_tasks(reason: str) -> dict:
    suites = {}
    for name, fname in SUITE_FILES.items():
        path = ROOT / fname
        if path.exists():
            tasks = load_tasks(path)
            suites[name] = {"n_tasks": len(tasks), "n_guardrail": sum(t.category == "guardrail" for t in tasks)}
    return pending_data(suites=suites, providers=len(_PROVIDERS) - 1, reason=reason, site_url=site_url())


def outputs(data: dict) -> list[tuple[pathlib.Path, str]]:
    return generated_files(ROOT, data)


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
    if data.get("site_url", "") != site_url():  # config-derived, so kept current here rather than by a results run
        if args.check:
            stale.append(f"docs/data/results.json: site_url is {data.get('site_url')!r}, release.yaml says {site_url()!r}")
        else:
            data["site_url"] = site_url()
            DATA.write_text(json.dumps(data, indent=2) + "\n")
            print(f"updated {DATA.relative_to(ROOT)} (site_url)", file=sys.stderr)
    for path, rendered in outputs(data):
        current = path.read_text() if path.exists() else ""
        if rendered == current:
            continue
        if args.check:
            diff = difflib.unified_diff(current.splitlines(), rendered.splitlines(), "committed", "rendered", lineterm="", n=1)
            stale.append(f"{path.relative_to(ROOT)}\n" + "\n".join(list(diff)[:30]))
        else:
            path.write_text(rendered)
            print(f"updated {path.relative_to(ROOT)} ({data['status']})", file=sys.stderr)
    if stale:
        print("Generated files are out of date with docs/data/results.json / release.yaml (edited by hand, or data changed).\n"
              "Run: python scripts/build_site.py\n\n" + "\n\n".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
