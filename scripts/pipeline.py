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
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval_agents.pipeline import EXIT_OK, EXIT_WAITING, Pipeline, Release  # noqa: E402


def sync_repo_metadata() -> None:
    """Best effort: keep the repo's description/topics/homepage in step with release.yaml. It needs the
    machine's `gh` login (admin), so it can't run in CI; a failure here never fails a release."""
    import subprocess

    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_repo.py")], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    print(f"[pipeline] {(done.stdout or done.stderr).strip().splitlines()[-1] if (done.stdout or done.stderr).strip() else 'repo sync: no output'}",
          file=sys.stderr, flush=True)


def write_launch_kit() -> None:
    """Best effort: refresh the announcement drafts in launch/kit/ from what was just published. It only writes
    files (a person posts them), and a failure here never fails a release."""
    import subprocess

    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "launch_kit.py")], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = (done.stdout if done.returncode == 0 else done.stderr).strip().splitlines()
    print(f"[pipeline] launch kit: {'refreshed in launch/kit/' if done.returncode == 0 else (out[0] if out else 'failed')}", file=sys.stderr, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--release", default=str(ROOT / "release.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--publish", action="store_true", help="commit and push published results")
    t.add_argument("--loop", action="store_true", help="repeat while waiting on a quota; stop when published or blocked")
    t.add_argument("--interval", type=int, default=900, help="seconds between attempts in --loop mode (default 900)")
    t.add_argument("--max-hours", type=float, default=48, help="give up looping after this long (default 48)")
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
    # The deadline survives the re-exec below, so --max-hours bounds the whole loop.
    deadline = float(os.environ.setdefault("PIPELINE_DEADLINE", str(time.time() + args.max_hours * 3600)))
    while True:
        # A fresh Pipeline each pass: state on disk is the truth, so a crash or edit between passes is harmless.
        pipe = Pipeline(Release.load(pathlib.Path(args.release)), ROOT)
        result = pipe.tick(publish=args.publish)
        print(f"[pipeline] {result.state}" + (f" — waiting on: {'; '.join(result.waiting_on)}" if result.waiting_on else "")
              + (f" — problems: {'; '.join(result.problems)}" if result.problems else ""), file=sys.stderr, flush=True)
        if not args.loop or result.exit_code != EXIT_WAITING:
            if result.state in ("published", "current"):
                sync_repo_metadata()
                write_launch_kit()
            return result.exit_code
        if time.time() + args.interval > deadline:
            print(f"[pipeline] still waiting after {args.max_hours}h; giving up (run tick again to continue)", file=sys.stderr)
            return result.exit_code
        time.sleep(args.interval)
        # Re-exec so the next pass runs the code as it is on disk now (this process imported the old code);
        # otherwise an unattended loop would keep publishing with whatever was checked out when it started.
        lock.close()
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    sys.exit(main())
