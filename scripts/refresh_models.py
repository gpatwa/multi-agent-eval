"""Refresh docs/data/models.json from the vendors' own pages, then re-render the Models page.

    python scripts/refresh_models.py            # fetch, rewrite models.json if it changed, rebuild docs/models.html
    python scripts/refresh_models.py --check    # fetch only; exit 1 if models.json is out of date (no writes)

Exit codes: 0 up to date or refreshed, 1 out of date (--check), 3 a vendor page could not be read (nothing
half-written). Anthropic rows come from its model pages; OpenAI and Google pages are watched, and any model ID
they list that models.json does not know is reported under "unreviewed" and shown on the page as such.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.refresh_models import dump, refresh  # noqa: E402

PATH = ROOT / "docs" / "data" / "models.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    current = json.loads(PATH.read_text())
    new, report = refresh(current)
    for p in report["problems"]:
        print(f"[refresh-models] problem: {p}")
    if report["problems"] and not report["anthropic_rows"]:
        return 3
    for vendor, ids in report["unreviewed"].items():
        print(f"[refresh-models] unreviewed on {vendor}'s page: {', '.join(ids)}")
    stale = dump(new) != dump(current)
    print(f"[refresh-models] changed: {report['changed'] or 'none'}; checked {new['checked']}")
    if args.check:
        return 1 if stale else 0
    if stale:
        PATH.write_text(dump(new))
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build_site.py")], check=True)
    return 3 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
