"""Run the results release pipeline (run -> merge -> second judge -> gate -> publish).

    python scripts/pipeline.py tick [--publish]   # do whatever is pending, then stop
    python scripts/pipeline.py status             # what's done, what's blocked, and why
    python scripts/pipeline.py adopt SUITE DIR    # count an existing run directory toward a suite

`tick` is idempotent and never sleeps: on a quota stop it records a cooldown and exits 75, and
whatever schedules it (a loop, launchd, the desktop app's scheduler) simply runs it again. Only
`--publish` commits and pushes (CI deploys on push); without it the data and page are updated
locally. See eval_agents/pipeline.py for the stages and exit codes.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.pipeline import EXIT_OK, Pipeline, Release  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--release", default=str(ROOT / "release.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--publish", action="store_true", help="commit and push published results")
    sub.add_parser("status")
    a = sub.add_parser("adopt")
    a.add_argument("suite")
    a.add_argument("dir", type=pathlib.Path)
    args = ap.parse_args(argv)

    pipe = Pipeline(Release.load(pathlib.Path(args.release)), ROOT)
    pipe.home.mkdir(parents=True, exist_ok=True)
    if args.cmd == "adopt":
        if args.suite not in pipe.r.suites:
            sys.exit(f"unknown suite {args.suite!r}; release has {list(pipe.r.suites)}")
        pipe.adopt(args.suite, args.dir.resolve())
        print(f"adopted {args.dir} into {args.suite}")
        return EXIT_OK
    if args.cmd == "status":
        for suite in pipe.r.suites:
            best, latest = pipe.plan(suite)
            print(f"{suite}:")
            for c in pipe.r.candidates:
                h = (latest[c] or (None, None))[1]
                print(f"  {c:8s} " + (f"ready ({best[c]})" if best[c] else f"needs run — {h['why']}: {h['detail']}" if h else "needs run — no run yet"))
        cool = {k: v for k, v in pipe.state["cooldown"].items() if v > pipe.now()}
        print(f"cooldowns: {cool or 'none'}")
        st = pipe.home / "status.json"
        print(f"last tick: {json.loads(st.read_text()) if st.exists() else 'never'}")
        return EXIT_OK

    lock = open(pipe.home / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another tick is running; nothing to do", file=sys.stderr)
        return EXIT_OK
    result = pipe.tick(publish=args.publish)
    print(f"[pipeline] {result.state}" + (f" — waiting on: {'; '.join(result.waiting_on)}" if result.waiting_on else "")
          + (f" — problems: {'; '.join(result.problems)}" if result.problems else ""), file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
