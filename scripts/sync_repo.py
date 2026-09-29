"""Keep the GitHub repo's public metadata (description, topics, homepage) in sync with release.yaml.

    python scripts/sync_repo.py [--dry-run]

Idempotent: reads the current values and only writes what differs. The homepage is set only once
the site answers with the landing page, so the repo never links to a dead URL. Uses the `gh` CLI's
existing login; changing repo settings needs admin rights, which CI's GITHUB_TOKEN lacks, so this
runs from the machine that runs the pipeline.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKER = "The Model Ledger"
USER_AGENT = "multi-agent-eval-sync/1.0 (+https://github.com/gpatwa/multi-agent-eval)"


def gh(args: list[str], stdin: str | None = None) -> tuple[int, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, input=stdin, stdin=None if stdin else subprocess.DEVNULL)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stdout + p.stderr).strip()


def site_is_live(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # Cloudflare blocks the default one
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200 and MARKER in r.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False


def normalize_topics(topics: list[str]) -> list[str]:
    """GitHub topics: lowercase, hyphenated, <= 50 chars, at most 20."""
    out = []
    for t in topics:
        t = t.strip().lower().replace("_", "-").replace(" ", "-")[:50]
        if t and t not in out:
            out.append(t)
    return out[:20]


def sync(desired: dict, *, gh_fn=gh, live_fn=site_is_live, dry_run: bool = False, log=print) -> list[str]:
    """Apply `desired` (description/homepage/topics); returns the changes made (or that would be)."""
    code, out = gh_fn(["repo", "view", "--json", "nameWithOwner,description,homepageUrl,repositoryTopics"])
    if code != 0:
        raise RuntimeError(f"gh repo view failed: {out}")
    cur = json.loads(out)
    slug = cur["nameWithOwner"]
    have_topics = sorted(t["name"] for t in (cur.get("repositoryTopics") or []))
    changes, patch = [], {}
    if desired.get("description") and cur.get("description") != desired["description"]:
        patch["description"] = desired["description"]
        changes.append("description")
    home = desired.get("homepage")
    if home and (cur.get("homepageUrl") or "").rstrip("/") != home.rstrip("/"):
        if live_fn(home):
            patch["homepage"] = home
            changes.append("homepage")
        else:
            log(f"homepage not set: {home} isn't serving the landing page yet")
    want = normalize_topics(desired.get("topics", []))
    topics_change = bool(want) and sorted(want) != have_topics
    if topics_change:
        changes.append("topics")
    if dry_run or not changes:
        return changes
    if patch:
        code, out = gh_fn(["api", "-X", "PATCH", f"repos/{slug}", *[a for k, v in patch.items() for a in ("-f", f"{k}={v}")]])
        if code != 0:
            raise RuntimeError(f"updating repo failed: {out}")
    if topics_change:
        code, out = gh_fn(["api", "-X", "PUT", f"repos/{slug}/topics", "--input", "-"], stdin=json.dumps({"names": want}))
        if code != 0:
            raise RuntimeError(f"updating topics failed: {out}")
    return changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--release", default=str(ROOT / "release.yaml"))
    args = ap.parse_args(argv)
    release = yaml.safe_load(pathlib.Path(args.release).read_text()) or {}
    desired = {**(release.get("repo") or {})}
    if (release.get("site") or {}).get("url") and "homepage" not in desired:
        desired["homepage"] = release["site"]["url"]  # the site URL is the repo's homepage; one source
    changes = sync(desired, dry_run=args.dry_run)
    verb = "would change" if args.dry_run else "changed"
    print(f"repo metadata: {verb} {', '.join(changes)}" if changes else "repo metadata: already in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
