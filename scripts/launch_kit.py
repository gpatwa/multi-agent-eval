#!/usr/bin/env python3
"""Write channel-ready announcement drafts from the published results.

    python scripts/launch_kit.py                    # drafts into launch/kit/ (never posts anything)
    python scripts/launch_kit.py --github-release   # also create a GitHub release with the notes (public; ask first)

Every number and model name comes from docs/data/results.json and history.json, and the limits are appended
by code (eval_agents/launch.py), so a draft can't claim more than the data supports. Exit 2 when the results
aren't ready to announce. See launch/PLAYBOOK.md for where to post and who posts.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.history import load as load_history  # noqa: E402
from eval_agents.launch import kit, readiness, x_posts  # noqa: E402
from eval_agents.launch import X_POST_MAX  # noqa: E402


def release_command(data: dict, notes: pathlib.Path) -> list[str]:
    tag = f"results-{data['generated'][:10]}"
    return ["gh", "release", "create", tag, "--target", "main", "--title", f"Results {data['generated'][:10]}",
            "--notes-file", str(notes)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "launch" / "kit")
    ap.add_argument("--github-release", action="store_true", help="create a public GitHub release from the notes (needs your go-ahead)")
    args = ap.parse_args(argv)

    data = json.loads((ROOT / "docs" / "data" / "results.json").read_text())
    history = load_history(ROOT / "docs" / "data" / "history.json")
    problems = readiness(data, history)
    if problems:
        print("not ready to announce:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2
    long = [p for p in x_posts(data, history) if len(p) > X_POST_MAX]
    if long:
        print(f"{len(long)} X post(s) exceed {X_POST_MAX} characters; shorten before posting", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in kit(data, history).items():
        (args.out / name).write_text(text)
        print(f"wrote {(args.out / name).relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out / name}")
    if args.github_release:
        cmd = release_command(data, args.out / "release_notes.md")
        print("running:", " ".join(cmd))
        return subprocess.run(cmd, cwd=ROOT).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
