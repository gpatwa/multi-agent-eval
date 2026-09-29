"""Deploy the landing page (docs/) to Cloudflare Pages at the site URL in release.yaml — end to end.

Idempotent; safe to run on every push (CI does: .github/workflows/deploy-site.yml):

  1. ensure the Cloudflare Pages project exists (direct-upload project),
  2. upload docs/ as a production deployment (wrangler),
  3. attach the custom domain to the project (this MUST precede the DNS record,
     or Cloudflare serves 522s for the hostname),
  4. ensure the DNS record  <domain> CNAME <project>.pages.dev  in the domain's
     Cloudflare zone (found by lookup in the same account),
  5. verify the live URL serves this page, then notify IndexNow search engines.

The domain and project come from `site:` in release.yaml (SITE_DOMAIN / CF_PAGES_PROJECT override).
With no domain the site is published at its <project>.pages.dev address only.

Usage:
  python scripts/deploy_site.py deploy [--dry-run] [--no-verify]
  python scripts/deploy_site.py status
  python scripts/deploy_site.py doctor      # read-only: what's ready, what's missing, and exactly what to do
  python scripts/deploy_site.py bootstrap   # only needed on a new repo: store the Cloudflare credentials as GitHub secrets

Credentials (environment variables; CI reads them from GitHub secrets):
  CLOUDFLARE_API_TOKEN   permissions: Account > Cloudflare Pages > Edit; Zone > DNS > Edit; Zone > Zone > Read
  CLOUDFLARE_ACCOUNT_ID

That is the whole credential surface. Creating them can't be automated (nobody else can issue you a token); `bootstrap`
stores them with hidden prompts so they never appear in shell history, files, or logs.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "docs"


def _site_config() -> dict:
    try:
        return (yaml.safe_load((ROOT / "release.yaml").read_text()) or {}).get("site") or {}
    except OSError:
        return {}


_SITE = _site_config()
DOMAIN = os.environ.get("SITE_DOMAIN") or urllib.parse.urlparse(_SITE.get("url", "")).hostname or ""
PROJECT = os.environ.get("CF_PAGES_PROJECT") or _SITE.get("project") or "model-ledger"
BRANCH = "main"
VERIFY_MARKER = "The Model Ledger"  # text the live page must contain

CF_API = "https://api.cloudflare.com/client/v4"

SECRETS = [
    ("CLOUDFLARE_API_TOKEN", True, "Cloudflare API token (Account > Cloudflare Pages > Edit; Zone > DNS > Edit; Zone > Zone > Read)"),
    ("CLOUDFLARE_ACCOUNT_ID", False, "Cloudflare account ID (dashboard sidebar)"),
]


class DeployError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[deploy-site] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP

# Cloudflare's bot protection rejects Python's default User-Agent (HTTP 403, error 1010) on the very sites it
# serves, so every request identifies itself.
USER_AGENT = "multi-agent-eval-deploy/1.0 (+https://github.com/gpatwa/multi-agent-eval)"


def http(method: str, url: str, *, headers=None, data: bytes | None = None, timeout=30):
    """Returns (status, body_text). Never raises on HTTP error statuses."""
    req = urllib.request.Request(url, method=method, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DeployError(f"missing {name} — run `python scripts/deploy_site.py doctor`")
    return value


def _call(request, token: str, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    """A Cloudflare API call returning (status, parsed JSON)."""
    data = json.dumps(body).encode() if body is not None else None
    status, text = request(method, url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=data)
    try:
        return status, json.loads(text) if text else {}
    except json.JSONDecodeError:
        return status, {"raw": text}


def _lazy_http():
    # Looked up at call time (not bound as a default argument) so tests can replace `http`.
    return lambda *a, **k: http(*a, **k)


# ---------------------------------------------------------------- Cloudflare Pages


class Cloudflare:
    def __init__(self, token: str, account_id: str, request=None):
        self._token = token
        self._base = f"{CF_API}/accounts/{account_id}/pages/projects"
        self._request = request or _lazy_http()

    def _call(self, method: str, path: str = "", body: dict | None = None) -> tuple[int, dict]:
        return _call(self._request, self._token, method, self._base + path, body)

    def ensure_project(self, dry_run=False) -> str:
        status, payload = self._call("GET", f"/{PROJECT}")
        if status == 200:
            subdomain = payload["result"]["subdomain"]
            log(f"Pages project {PROJECT!r} exists ({subdomain})")
            return subdomain
        if status != 404:
            raise DeployError(f"Cloudflare GET project failed ({status}): {payload}")
        if dry_run:
            log(f"would create Pages project {PROJECT!r}")
            return f"{PROJECT}.pages.dev"
        status, payload = self._call("POST", "", {"name": PROJECT, "production_branch": BRANCH})
        if status not in (200, 201):
            raise DeployError(f"Cloudflare create project failed ({status}): {payload}")
        subdomain = payload["result"]["subdomain"]
        log(f"created Pages project {PROJECT!r} ({subdomain})")
        return subdomain

    def ensure_domain(self, dry_run=False) -> dict:
        status, payload = self._call("GET", f"/{PROJECT}/domains/{DOMAIN}")
        if status == 200:
            log(f"custom domain {DOMAIN} attached (status: {payload['result'].get('status')})")
            return payload["result"]
        if status != 404:
            raise DeployError(f"Cloudflare GET domain failed ({status}): {payload}")
        if dry_run:
            log(f"would attach custom domain {DOMAIN}")
            return {"name": DOMAIN, "status": "pending"}
        status, payload = self._call("POST", f"/{PROJECT}/domains", {"name": DOMAIN})
        if status not in (200, 201):
            raise DeployError(f"Cloudflare attach domain failed ({status}): {payload}")
        log(f"attached custom domain {DOMAIN} (status: {payload['result'].get('status')})")
        return payload["result"]

    def domain_status(self) -> str | None:
        status, payload = self._call("GET", f"/{PROJECT}/domains/{DOMAIN}")
        return payload["result"].get("status") if status == 200 else None


def upload_site(dry_run=False) -> None:
    """Production deployment of docs/ via wrangler (Cloudflare's supported uploader)."""
    npx = shutil.which("npx")
    if not npx:
        raise DeployError("npx (Node.js) is required to run wrangler")
    cmd = [npx, "--yes", "wrangler@4", "pages", "deploy", str(SITE_DIR),
           f"--project-name={PROJECT}", f"--branch={BRANCH}", "--commit-dirty=true"]
    if dry_run:
        log("would run: " + " ".join(cmd[1:]))
        return
    log("uploading docs/ with wrangler …")
    result = subprocess.run(cmd, env={**os.environ}, stdin=subprocess.DEVNULL, text=True, capture_output=True)
    sys.stdout.write(result.stdout[-2000:])
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-4000:])
        raise DeployError(f"wrangler pages deploy failed (exit {result.returncode})")


# ---------------------------------------------------------------- Cloudflare DNS


def plan_dns(records: list[dict], fqdn: str, target: str) -> tuple[str, dict | None]:
    """What to do about `fqdn`: ('keep'|'create'|'update'|'conflict', existing record).

    Only a record we can recognise as ours is ever changed: a CNAME already pointing at a Pages address.
    Any other record at the name (an A record, a CNAME elsewhere, TXT ...) belongs to someone, so it's a
    conflict to report, never something to overwrite."""
    same = [r for r in records if r["name"].lower() == fqdn.lower()]
    if not same:
        return "create", None
    if len(same) == 1 and same[0]["type"] == "CNAME":
        r = same[0]
        content = r["content"].rstrip(".").lower()
        if content == target.lower() and r.get("proxied"):
            return "keep", r
        if content.endswith(".pages.dev"):
            return "update", r
    return "conflict", same[0]


class CloudflareDNS:
    def __init__(self, token: str, request=None):
        self._token = token
        self._request = request or _lazy_http()

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        return _call(self._request, self._token, method, f"{CF_API}{path}", body)

    def find_zone(self, domain: str) -> dict | None:
        """The zone (in this account) that hosts `domain`: the longest zone name that is a suffix of it."""
        status, payload = self._call("GET", "/zones?per_page=50")
        if status != 200:
            raise DeployError(f"Cloudflare list zones failed ({status}): {payload.get('errors') or payload}")
        zones = [z for z in payload.get("result") or [] if domain == z["name"] or domain.endswith("." + z["name"])]
        return max(zones, key=lambda z: len(z["name"])) if zones else None

    def ensure_cname(self, domain: str, target: str, dry_run=False) -> str:
        """Point `domain` at `target`. Returns 'keep'|'create'|'update'|'no-zone'."""
        zone = self.find_zone(domain)
        if zone is None:
            log(f"WARNING: {domain} isn't in a zone on this Cloudflare account, so DNS can't be set here. "
                f"Create a CNAME {domain} -> {target} at the DNS host.")
            return "no-zone"
        status, payload = self._call("GET", f"/zones/{zone['id']}/dns_records?name={domain}")
        if status != 200:
            raise DeployError(_dns_denied("read", zone["name"], status, payload))
        action, existing = plan_dns(payload.get("result") or [], domain, target)
        if action == "conflict":
            raise DeployError(f"{domain} already has a {existing['type']} record ({existing['content']}); refusing to overwrite "
                              "a record that isn't a Pages CNAME. Remove it or choose another subdomain in release.yaml.")
        if action == "keep":
            log(f"DNS ok: {domain} CNAME {target} (proxied)")
            return "keep"
        record = {"type": "CNAME", "name": domain, "content": target, "proxied": True, "ttl": 1,
                  "comment": "managed by scripts/deploy_site.py"}
        if dry_run:
            log(f"would {action} DNS: {domain} CNAME {target}")
            return action
        method, path = ("POST", f"/zones/{zone['id']}/dns_records") if action == "create" else \
                       ("PATCH", f"/zones/{zone['id']}/dns_records/{existing['id']}")
        status, payload = self._call(method, path, record)
        if status not in (200, 201):
            raise DeployError(_dns_denied("write", zone["name"], status, payload))
        log(f"DNS {action}d: {domain} CNAME {target} (proxied)")
        return action


def _dns_denied(what: str, zone: str, status: int, payload: dict) -> str:
    hint = " The API token needs 'Zone > DNS > Edit' (and 'Zone > Zone > Read') on this zone." if status in (401, 403) else ""
    return f"Cloudflare DNS {what} for {zone} failed ({status}): {payload.get('errors') or payload}.{hint}"


# ---------------------------------------------------------------- verify


def verify(cf: Cloudflare, url: str | None = None, check_domain: bool = True, timeout_s: int = 900) -> bool:
    """Wait until `url` serves the landing page (and, for the custom domain, Cloudflare reports it
    active). A first deploy waits on DNS propagation + certificate issuance; returns False (not an
    error) if that's still pending at the timeout — the next run re-verifies."""
    url = url or f"https://{DOMAIN}/"
    deadline = time.time() + timeout_s
    domain_state = page_ok = None
    status = None
    while time.time() < deadline:
        domain_state = cf.domain_status() if check_domain else "n/a"
        if domain_state in ("active", "n/a"):
            status, body = http("GET", url, timeout=15)
            page_ok = status == 200 and VERIFY_MARKER in body
            if page_ok:
                log(f"live: {url} serves the landing page")
                return True
        log(f"waiting: domain {domain_state or 'unknown'}, {url} -> HTTP {status if status is not None else 'not requested'}"
            f"{' (marker missing)' if status == 200 else ''} …")
        time.sleep(30)
    log(f"not live yet (domain {domain_state}); DNS/TLS can take longer on first setup — the next run re-verifies")
    return False


# ---------------------------------------------------------------- search engines

INDEXNOW = "https://api.indexnow.org/indexnow"
_KEY_FILE = re.compile(r"^[0-9a-f]{32}\.txt$")


def indexnow_key(domain: str = "") -> str:
    """IndexNow's ownership key is public by design (it is served from the site itself), so a
    deterministic one derived from the domain needs no secret storage."""
    return hashlib.sha256(f"indexnow:{domain or DOMAIN}".encode()).hexdigest()[:32]


def ensure_indexnow_key_file(site_dir: pathlib.Path = SITE_DIR, domain: str = "") -> pathlib.Path:
    """Write the key file for `domain` and remove key files left from a previous domain."""
    key = indexnow_key(domain)
    path = site_dir / f"{key}.txt"
    if not path.exists() or path.read_text().strip() != key:
        path.write_text(key + "\n")
    for stale in site_dir.iterdir():
        if _KEY_FILE.match(stale.name) and stale != path:
            stale.unlink()
    return path


def ping_indexnow(urls: list[str], domain: str = "", request=None) -> bool:
    """Tell IndexNow-participating engines (Bing, Yandex, Seznam, Naver...) the URLs changed. Google
    doesn't participate; it finds the site through the sitemap and links to it."""
    domain = domain or DOMAIN
    body = json.dumps({"host": domain, "key": indexnow_key(domain), "keyLocation": f"https://{domain}/{indexnow_key(domain)}.txt",
                       "urlList": urls}).encode()
    status, _ = (request or _lazy_http())("POST", INDEXNOW, headers={"Content-Type": "application/json; charset=utf-8"}, data=body)
    log(f"IndexNow ping -> HTTP {status}")
    return status in (200, 202)


# ---------------------------------------------------------------- commands


def cmd_deploy(args) -> int:
    token, account = _env("CLOUDFLARE_API_TOKEN"), _env("CLOUDFLARE_ACCOUNT_ID")
    cf = Cloudflare(token, account)
    if DOMAIN:
        ensure_indexnow_key_file()  # must exist before the upload so the engines can fetch it
    pages_host = cf.ensure_project(dry_run=args.dry_run)
    upload_site(dry_run=args.dry_run)
    if not DOMAIN:
        log(f"no site domain configured: published at https://{pages_host}/ only")
        if not (args.dry_run or args.no_verify):
            verify(cf, url=f"https://{pages_host}/", check_domain=False)
        return 0
    cf.ensure_domain(dry_run=args.dry_run)  # before DNS, or Cloudflare 522s the hostname
    dns = CloudflareDNS(token).ensure_cname(DOMAIN, pages_host, dry_run=args.dry_run)
    if args.dry_run or args.no_verify:
        return 0
    live = dns != "no-zone"
    if verify(cf, url=f"https://{DOMAIN}/" if live else f"https://{pages_host}/", check_domain=live) and live:
        ping_indexnow([f"https://{DOMAIN}/"])
    return 0


def cmd_status(_args) -> int:
    cf = Cloudflare(_env("CLOUDFLARE_API_TOKEN"), _env("CLOUDFLARE_ACCOUNT_ID"))
    host = cf.ensure_project(dry_run=True)
    for url in [f"https://{host}/"] + ([f"https://{DOMAIN}/"] if DOMAIN else []):
        status, body = http("GET", url, timeout=15)
        log(f"{url} -> {status}{' (landing page)' if VERIFY_MARKER in body else ''}")
    if DOMAIN:
        log(f"domain {DOMAIN}: {cf.domain_status() or 'not attached'}")
    return 0


def cmd_doctor(_args) -> int:
    """Read-only readiness report. Exit 0 only when the whole path can run unattended."""
    rows, ok = [], True

    def row(label, good, detail):
        nonlocal ok
        ok &= bool(good)
        rows.append(f"  {'ok     ' if good else 'MISSING'}  {label:38s} {detail}")

    try:
        listed = subprocess.run(["gh", "secret", "list"], capture_output=True, text=True, cwd=ROOT)
        in_gh = {line.split()[0] for line in listed.stdout.splitlines() if line.strip()} if listed.returncode == 0 else set()
    except OSError:
        in_gh = set()
    for name, _, help_text in SECRETS:
        have = bool(os.environ.get(name))
        row(f"credential {name}", have or name in in_gh,
            "GitHub secret set" if name in in_gh else "in this environment" if have else help_text)
    token, account = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    host = f"{PROJECT}.pages.dev"
    if token:
        status, _ = http("GET", f"{CF_API}/user/tokens/verify", headers={"Authorization": f"Bearer {token}"})
        row("Cloudflare token is valid", status == 200, f"HTTP {status}")
        if account:
            cf = Cloudflare(token, account)
            status, payload = cf._call("GET", f"/{PROJECT}")
            row(f"Pages project {PROJECT}", status == 200, "exists" if status == 200 else "created by the first deploy")
            host = (payload.get("result") or {}).get("subdomain", host)
        if DOMAIN:
            try:
                zone = CloudflareDNS(token).find_zone(DOMAIN)
                row(f"zone for {DOMAIN} on this account", zone, zone["name"] if zone else "not found: DNS must be set by hand")
            except DeployError as exc:
                row(f"zone for {DOMAIN} on this account", False, str(exc)[:110])
    status, body = http("GET", f"https://{host}/", timeout=10)
    row(f"https://{host}/ serves the page", status == 200 and VERIFY_MARKER in body, f"HTTP {status}")
    if DOMAIN:
        status, body = http("GET", f"https://{DOMAIN}/", timeout=10)
        row(f"https://{DOMAIN}/ serves the page", status == 200 and VERIFY_MARKER in body, f"HTTP {status}" if status else "not reachable")
    print("\n".join(rows))
    print("\nReady: every step runs unattended." if ok else
          "\nNot ready yet. The only step that can't be automated is creating the Cloudflare credentials; "
          "`python scripts/deploy_site.py bootstrap` stores them once. Everything else follows from them.")
    return 0 if ok else 1


def cmd_bootstrap(_args) -> int:
    """Prompt (hidden input) and store each credential as a GitHub Actions secret."""
    if not shutil.which("gh"):
        raise DeployError("GitHub CLI `gh` is required (and `gh auth login`)")
    print("Values are sent straight to GitHub secrets, never echoed or saved.")
    for name, hidden, help_text in SECRETS:
        prompt = f"{name} — {help_text}: "
        value = getpass.getpass(prompt) if hidden else input(prompt)
        if not value.strip():
            print(f"  skipped {name} (empty)")
            continue
        subprocess.run(["gh", "secret", "set", name], input=value.strip(), text=True, check=True, cwd=ROOT, capture_output=True)
        print(f"  stored {name}")
    print("Done. Pushes to main that touch docs/ now deploy automatically.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("deploy")
    d.add_argument("--dry-run", action="store_true", help="read-only: report what would change")
    d.add_argument("--no-verify", action="store_true", help="skip waiting for the live URL")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("bootstrap")
    args = parser.parse_args(argv)
    try:
        return {"deploy": cmd_deploy, "status": cmd_status, "bootstrap": cmd_bootstrap, "doctor": cmd_doctor}[args.command](args)
    except DeployError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
