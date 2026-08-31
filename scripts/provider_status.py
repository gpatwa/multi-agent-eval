#!/usr/bin/env python3
"""One command to answer "which providers are ready to use right now" --
replaces the one-off audit scripts written by hand three separate times
during this project's early development.

    python scripts/provider_status.py            # fast: env vars + CLI presence only
    python scripts/provider_status.py --live      # also live-probes installed CLIs
                                                   # (slow: each call is a real
                                                   # subprocess costing 5-90s and
                                                   # counting against your quota)

Exit code is 0 if at least one provider is ready, 1 if none are (mirrors
the "No candidates available" failure mode in eval_agents/config.py).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval_agents.registry import _PROVIDERS  # noqa: E402

_CLI_BINARY = {"claude-code": "claude", "codex-cli": "codex", "gemini-cli": "gemini"}
_CLI_PROBE_ARGS = {
    "claude-code": ["-p", "ok", "--model", "haiku", "--output-format", "json"],
    "codex-cli": ["exec", "--skip-git-repo-check", "ok"],
    "gemini-cli": ["-p", "ok"],
}


def check_one(provider: str, live: bool) -> tuple[str, str]:
    """Return (status, detail). status is one of: ready, pending, n/a."""
    _, _, env_var = _PROVIDERS[provider]

    if provider == "mock":
        return "ready", "offline, always available"

    if provider in _CLI_BINARY:
        binary = _CLI_BINARY[provider]
        path = shutil.which(binary)
        if not path:
            return "pending", f"'{binary}' CLI not installed"
        if not live:
            return "ready?", f"'{binary}' installed at {path} — auth unverified, use --live to confirm"
        try:
            proc = subprocess.run(
                [path, *_CLI_PROBE_ARGS[provider]], capture_output=True, text=True, timeout=90
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0 and "error" not in out.lower()[:80]:
                return "ready", "live probe succeeded"
            return "pending", f"live probe failed: {out.strip()[:100]}"
        except subprocess.TimeoutExpired:
            return "pending", "live probe timed out after 90s"

    if provider == "local":
        base = os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1")
        try:
            import urllib.request

            urllib.request.urlopen(base.rsplit("/v1", 1)[0] + "/api/version", timeout=2)
            return "ready", f"server responding at {base}"
        except Exception as exc:
            return "pending", f"no server at {base} ({type(exc).__name__}) — e.g. `ollama serve`"

    # API-key providers
    if env_var and os.environ.get(env_var):
        return "ready", f"{env_var} is set"
    if provider == "anthropic":
        # anthropic also accepts an `ant auth login` CLI profile
        return "ready?", f"{env_var} not set — may still work via `ant auth login` profile"
    return "pending", f"{env_var} not set"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="live-probe installed vendor CLIs (slow, costs quota)")
    args = parser.parse_args()

    rows = [(p, *check_one(p, args.live)) for p in sorted(_PROVIDERS)]
    width = max(len(p) for p in _PROVIDERS)
    ready_count = 0
    for provider, status, detail in rows:
        if status.startswith("ready"):
            ready_count += 1
        marker = {"ready": "✅", "ready?": "❓", "pending": "⏳"}[status]
        print(f"{marker} {provider:<{width}}  {detail}")

    print(f"\n{ready_count}/{len(rows)} providers ready" + ("" if args.live else " (add --live to verify CLI auth)"))
    return 0 if ready_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
