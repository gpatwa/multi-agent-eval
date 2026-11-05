#!/usr/bin/env python3
"""Write channel-ready announcement drafts from the published results.

    python scripts/launch_kit.py                    # drafts into launch/kit/ (never posts anything)
    python scripts/launch_kit.py --github-release   # also create a DRAFT GitHub release (not public, no tag yet)
    python scripts/launch_kit.py --github-release --publish   # publish it (public; do this only with the owner's go-ahead)

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


def release_tag(data: dict) -> str:
    return f"results-{data['generated'][:10]}"


def release_command(data: dict, notes: pathlib.Path, *, draft: bool = True) -> list[str]:
    cmd = ["gh", "release", "create", release_tag(data), "--target", "main", "--title", f"Results {data['generated'][:10]}",
           "--notes-file", str(notes)]
    return cmd + (["--draft"] if draft else [])


def release_action(existing: dict | None, publish: bool) -> str:
    """What to do about a release that may already exist for this tag. Never edit or re-publish a public release."""
    if existing is None:
        return "create"
    if existing.get("isDraft"):
        return "publish-draft" if publish else "update-draft"
    return "refuse"


def _gh_release(tag: str) -> dict | None:
    done = subprocess.run(["gh", "release", "view", tag, "--json", "isDraft,url,tagName"], cwd=ROOT, capture_output=True, text=True)
    return json.loads(done.stdout) if done.returncode == 0 else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "launch" / "kit")
    ap.add_argument("--github-release", action="store_true", help="create (or update) a DRAFT GitHub release from the notes")
    ap.add_argument("--publish", action="store_true", help="with --github-release: make the release public (owner's go-ahead only)")
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
        tag, notes = release_tag(data), args.out / "release_notes.md"
        existing = _gh_release(tag)
        action = release_action(existing, args.publish)
        if action == "refuse":
            print(f"release {tag} is already public ({existing['url']}); not touching it", file=sys.stderr)
            return 1
        cmds = {"create": release_command(data, notes, draft=not args.publish),
                "update-draft": ["gh", "release", "edit", tag, "--notes-file", str(notes)],
                "publish-draft": ["gh", "release", "edit", tag, "--notes-file", str(notes), "--draft=false"]}
        cmd = cmds[action]
        print(f"{action}:", " ".join(cmd))
        return subprocess.run(cmd, cwd=ROOT).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
