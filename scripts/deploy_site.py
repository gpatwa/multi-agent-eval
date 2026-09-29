"""Deploy the landing page (docs/) to Cloudflare Pages at eval.aveto.com — end to end.

Idempotent; safe to run on every push (CI does: .github/workflows/deploy-site.yml):

  1. ensure the Cloudflare Pages project exists (direct-upload project),
  2. upload docs/ as a production deployment (wrangler),
  3. ensure the custom domain is attached to the project — this MUST precede
     the DNS record, or Cloudflare serves 522s for the hostname,
  4. ensure the DNS CNAME  <sub> -> <project>.pages.dev  at NIC.RU (the zone's
     DNS host), replacing a stale record and committing the zone,
  5. verify: Cloudflare reports the domain active and the live URL serves
     this page.

Usage:
  python scripts/deploy_site.py deploy [--dry-run] [--no-verify]
  python scripts/deploy_site.py status
  python scripts/deploy_site.py doctor      # what's ready, what's missing, and exactly what to do
  python scripts/deploy_site.py bootstrap   # one-time: store credentials as GitHub secrets

Credentials (environment variables; CI reads them from GitHub secrets):
  CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID   token needs "Cloudflare Pages: Edit"
  NICRU_USERNAME, NICRU_PASSWORD                 NIC.RU account (e.g. 123456/NIC-D)
  NICRU_CLIENT_ID, NICRU_CLIENT_SECRET           NIC.RU OAuth app (DNS-hosting API)

`bootstrap` prompts for these in your own terminal (hidden input) and stores
them with `gh secret set`, so they never appear in shell history, files, or logs.
It is the only step that can't be automated: something has to hold the first
credential. Everything after it runs unattended.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "docs"

DOMAIN = os.environ.get("SITE_DOMAIN", "eval.aveto.com")
ZONE = os.environ.get("SITE_ZONE", "aveto.com")
PROJECT = os.environ.get("CF_PAGES_PROJECT", "model-ledger")
BRANCH = "main"
VERIFY_MARKER = "The Model Ledger"  # text the live page must contain
CNAME_TTL = 300

CF_API = "https://api.cloudflare.com/client/v4"
NIC_API = "https://api.nic.ru"
NIC_SCOPE = ".+:/dns-master/.+"

SECRETS = [
    ("CLOUDFLARE_API_TOKEN", True, "Cloudflare API token (permission: Account > Cloudflare Pages > Edit)"),
    ("CLOUDFLARE_ACCOUNT_ID", False, "Cloudflare account ID (dashboard sidebar)"),
    ("NICRU_USERNAME", False, "NIC.RU account login (e.g. 123456/NIC-D)"),
    ("NICRU_PASSWORD", True, "NIC.RU account password (or the technical password for the API)"),
    ("NICRU_CLIENT_ID", False, "NIC.RU OAuth app ID (nic.ru/manager/oauth.cgi?step=oauth.app_register)"),
    ("NICRU_CLIENT_SECRET", True, "NIC.RU OAuth app secret"),
]


class DeployError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[deploy-site] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP


def http(method: str, url: str, *, headers=None, data: bytes | None = None, timeout=30):
    """Returns (status, body_text). Never raises on HTTP error statuses."""
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


NIC_VARS = ("NICRU_USERNAME", "NICRU_PASSWORD", "NICRU_CLIENT_ID", "NICRU_CLIENT_SECRET")


def nic_credentials() -> tuple[str, str, str, str] | None:
    """All four NIC.RU values, or None (DNS management is optional: see cmd_deploy)."""
    values = tuple(os.environ.get(n) for n in NIC_VARS)
    return values if all(values) else None


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DeployError(f"missing {name} — run `python scripts/deploy_site.py bootstrap` once")
    return value


# ---------------------------------------------------------------- Cloudflare Pages


class Cloudflare:
    def __init__(self, token: str, account_id: str, request=None):
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._base = f"{CF_API}/accounts/{account_id}/pages/projects"
        # Looked up at call time (not bound as a default argument) so tests can replace `http`.
        self._request = request or (lambda *a, **k: http(*a, **k))

    def _call(self, method: str, path: str = "", body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        status, text = self._request(method, self._base + path, headers=self._headers, data=data)
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        return status, payload

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
    env = {**os.environ}  # wrangler reads CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID
    result = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, text=True, capture_output=True)
    sys.stdout.write(result.stdout[-2000:])
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-4000:])
        raise DeployError(f"wrangler pages deploy failed (exit {result.returncode})")


# ---------------------------------------------------------------- NIC.RU DNS


def cname_label(domain: str, zone: str) -> str:
    """Record name relative to the zone: eval.aveto.com in aveto.com -> 'eval'."""
    if domain == zone or not domain.endswith("." + zone):
        raise DeployError(f"{domain} is not a subdomain of {zone}")
    return domain[: -len(zone) - 1]


def cname_xml(label: str, target: str, ttl: int = CNAME_TTL) -> str:
    rr = ElementTree.Element("rr")
    ElementTree.SubElement(rr, "name").text = label
    ElementTree.SubElement(rr, "ttl").text = str(ttl)
    ElementTree.SubElement(rr, "type").text = "CNAME"
    ElementTree.SubElement(ElementTree.SubElement(rr, "cname"), "name").text = target.rstrip(".") + "."
    inner = ElementTree.tostring(rr, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8" ?><request><rr-list>{inner}</rr-list></request>'


def parse_records(xml_text: str) -> list[dict]:
    """[{id, name, type, target}] from a NIC.RU records response."""
    root = ElementTree.fromstring(xml_text)
    out = []
    for rr in root.iter("rr"):
        target = rr.find("cname/name")
        out.append({
            "id": rr.attrib.get("id"),
            "name": (rr.findtext("name") or "").rstrip("."),
            "type": (rr.findtext("type") or "").upper(),
            "target": (target.text or "").rstrip(".").lower() if target is not None else None,
        })
    return out


def plan_dns(records: list[dict], label: str, target: str) -> dict:
    """Decide what to change: {'keep': bool, 'delete': [ids], 'add': bool}.

    A CNAME can't coexist with other records at the same name, so any other
    record at the label is removed along with a stale CNAME."""
    target = target.rstrip(".").lower()
    at_label = [r for r in records if r["name"].lower() == label.lower()]
    if len(at_label) == 1 and at_label[0]["type"] == "CNAME" and at_label[0]["target"] == target:
        return {"keep": True, "delete": [], "add": False}
    return {"keep": False, "delete": [r["id"] for r in at_label], "add": True}


class NicRu:
    def __init__(self, username, password, client_id, client_secret, request=None):
        request = request or (lambda *a, **k: http(*a, **k))
        self._request = request
        status, text = request(
            "POST", f"{NIC_API}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=urllib.parse.urlencode({
                "grant_type": "password", "username": username, "password": password,
                "client_id": client_id, "client_secret": client_secret, "scope": NIC_SCOPE,
            }).encode(),
        )
        if status != 200:
            raise DeployError(f"NIC.RU OAuth failed ({status}) — check NICRU_* credentials")
        self._auth = {"Authorization": f"Bearer {json.loads(text)['access_token']}"}

    def _call(self, method, path, data: str | None = None) -> str:
        headers = dict(self._auth)
        if data is not None:
            headers["Content-Type"] = "text/xml"
        status, text = self._request(method, f"{NIC_API}/dns-master/{path}", headers=headers,
                                     data=data.encode() if data is not None else None)
        if status != 200:
            raise DeployError(f"NIC.RU {method} {path} failed ({status}): {text[:300]}")
        return text

    def find_service(self, zone: str) -> str:
        """The NIC.RU DNS service that hosts `zone`."""
        services = ElementTree.fromstring(self._call("GET", "services"))
        for svc in services.iter("service"):
            name = svc.attrib.get("name")
            zones = ElementTree.fromstring(self._call("GET", f"services/{name}/zones"))
            if any(z.attrib.get("name", "").rstrip(".") == zone for z in zones.iter("zone")):
                return name
        raise DeployError(f"zone {zone} not found in any NIC.RU DNS-hosting service")

    def ensure_cname(self, zone: str, label: str, target: str, dry_run=False) -> None:
        service = self.find_service(zone)
        base = f"services/{service}/zones/{zone}"
        plan = plan_dns(parse_records(self._call("GET", f"{base}/records")), label, target)
        if plan["keep"]:
            log(f"DNS ok: {label}.{zone} CNAME {target}")
            return
        if dry_run:
            log(f"would replace records {plan['delete']} and add {label}.{zone} CNAME {target}")
            return
        for record_id in plan["delete"]:
            self._call("DELETE", f"{base}/records/{record_id}")
            log(f"deleted stale DNS record #{record_id} at {label}.{zone}")
        self._call("PUT", f"{base}/records", cname_xml(label, target))
        self._call("POST", f"{base}/commit")
        log(f"DNS set and committed: {label}.{zone} CNAME {target}")


# ---------------------------------------------------------------- verify


def verify(cf: Cloudflare, url: str | None = None, check_domain: bool = True, timeout_s: int = 900) -> bool:
    """Wait until `url` serves the landing page (and, for the custom domain, Cloudflare reports it
    active). A first deploy waits on DNS propagation + certificate issuance; returns False (not an
    error) if that's still pending at the timeout — the next run re-verifies."""
    url = url or f"https://{DOMAIN}/"
    deadline = time.time() + timeout_s
    domain_state = page_ok = None
    while time.time() < deadline:
        domain_state = cf.domain_status() if check_domain else "n/a"
        if domain_state in ("active", "n/a"):
            status, body = http("GET", url, timeout=15)
            page_ok = status == 200 and VERIFY_MARKER in body
            if page_ok:
                log(f"live: {url} serves the landing page")
                return True
        log(f"waiting: domain {domain_state or 'unknown'}, page {'ok' if page_ok else 'not yet'} …")
        time.sleep(30)
    log(f"not live yet (domain {domain_state}); DNS/TLS can take longer on first setup — "
        "the next run re-verifies")
    return False


# ---------------------------------------------------------------- search engines

INDEXNOW = "https://api.indexnow.org/indexnow"


def indexnow_key(domain: str = DOMAIN) -> str:
    """IndexNow's ownership key is public by design (it is served from the site itself), so a
    deterministic one derived from the domain needs no secret storage."""
    return hashlib.sha256(f"indexnow:{domain}".encode()).hexdigest()[:32]


def ensure_indexnow_key_file(site_dir: pathlib.Path = SITE_DIR, domain: str = DOMAIN) -> pathlib.Path:
    path = site_dir / f"{indexnow_key(domain)}.txt"
    if not path.exists() or path.read_text().strip() != indexnow_key(domain):
        path.write_text(indexnow_key(domain) + "\n")
    return path


def ping_indexnow(urls: list[str], domain: str = DOMAIN, request=http) -> bool:
    """Tell IndexNow-participating engines (Bing, Yandex, Seznam, Naver...) the URLs changed. Google
    doesn't participate; it finds the site through the sitemap and links to it."""
    body = json.dumps({"host": domain, "key": indexnow_key(domain), "keyLocation": f"https://{domain}/{indexnow_key(domain)}.txt",
                       "urlList": urls}).encode()
    status, _ = request("POST", INDEXNOW, headers={"Content-Type": "application/json; charset=utf-8"}, data=body)
    log(f"IndexNow ping -> HTTP {status}")
    return status in (200, 202)


# ---------------------------------------------------------------- commands


def cmd_deploy(args) -> int:
    cf = Cloudflare(_env("CLOUDFLARE_API_TOKEN"), _env("CLOUDFLARE_ACCOUNT_ID"))
    ensure_indexnow_key_file()  # must exist before the upload so the engines can fetch it
    pages_host = cf.ensure_project(dry_run=args.dry_run)
    upload_site(dry_run=args.dry_run)
    cf.ensure_domain(dry_run=args.dry_run)  # before DNS, or Cloudflare 522s the hostname
    creds = nic_credentials()
    if creds:
        NicRu(*creds).ensure_cname(ZONE, cname_label(DOMAIN, ZONE), pages_host, dry_run=args.dry_run)
    else:
        # Publishing needs only Cloudflare. Without DNS access the site is still live at its pages.dev address,
        # and the custom domain (already attached above) activates as soon as NIC.RU credentials exist.
        log(f"NIC.RU credentials not set: published at https://{pages_host}/ ; {DOMAIN} activates once DNS can be set")
    if args.dry_run or args.no_verify:
        return 0
    if verify(cf, url=f"https://{DOMAIN}/" if creds else f"https://{pages_host}/", check_domain=bool(creds)) and creds:
        ping_indexnow([f"https://{DOMAIN}/"])
    return 0


def cmd_doctor(_args) -> int:
    """Read-only readiness report. Exit 0 only when the whole path can run unattended."""
    import socket

    rows, ok = [], True

    def row(label, good, detail):
        nonlocal ok
        ok &= bool(good)
        rows.append(f"  {'ok     ' if good else 'MISSING'}  {label:34s} {detail}")

    have_env = {name: bool(os.environ.get(name)) for name, *_ in SECRETS}
    try:
        listed = subprocess.run(["gh", "secret", "list"], capture_output=True, text=True, cwd=ROOT)
        in_gh = {line.split()[0] for line in listed.stdout.splitlines() if line.strip()} if listed.returncode == 0 else set()
    except OSError:
        in_gh = set()
    for name, _, help_text in SECRETS:
        row(f"credential {name}", have_env[name] or name in in_gh,
            "GitHub secret set" if name in in_gh else "in this environment" if have_env[name] else help_text)
    if have_env["CLOUDFLARE_API_TOKEN"]:
        status, _ = http("GET", f"{CF_API}/user/tokens/verify", headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
        row("Cloudflare token is valid", status == 200, f"HTTP {status}")
    try:
        dns_ok = bool(socket.getaddrinfo(DOMAIN, 443))
    except OSError:
        dns_ok = False
    row(f"DNS for {DOMAIN}", dns_ok, "resolves" if dns_ok else "no record yet (created by `deploy` once credentials exist)")
    status, body = http("GET", f"https://{DOMAIN}/", timeout=10) if dns_ok else (0, "")
    row(f"https://{DOMAIN}/ serves the page", status == 200 and VERIFY_MARKER in body, f"HTTP {status}" if status else "not reachable")
    print("\n".join(rows))
    print("\nReady: every step runs unattended." if ok else
          "\nNot ready. The only steps that cannot be automated are creating credentials (nobody else can issue them for you);\n"
          "`python scripts/deploy_site.py bootstrap` stores them once. Everything after that is automatic.")
    return 0 if ok else 1


def cmd_status(_args) -> int:
    cf = Cloudflare(_env("CLOUDFLARE_API_TOKEN"), _env("CLOUDFLARE_ACCOUNT_ID"))
    log(f"domain {DOMAIN}: {cf.domain_status() or 'not attached'}")
    status, body = http("GET", f"https://{DOMAIN}/", timeout=15)
    log(f"https://{DOMAIN}/ -> {status}{' (landing page)' if VERIFY_MARKER in body else ''}")
    return 0


def cmd_bootstrap(_args) -> int:
    """Prompt (hidden input) and store each credential as a GitHub Actions secret."""
    if not shutil.which("gh"):
        raise DeployError("GitHub CLI `gh` is required (and `gh auth login`)")
    print("One-time setup: values are sent straight to GitHub secrets, never echoed or saved.")
    for name, hidden, help_text in SECRETS:
        prompt = f"{name} — {help_text}: "
        value = getpass.getpass(prompt) if hidden else input(prompt)
        if not value.strip():
            print(f"  skipped {name} (empty)")
            continue
        subprocess.run(["gh", "secret", "set", name], input=value.strip(), text=True, check=True,
                       cwd=ROOT, capture_output=True)
        print(f"  stored {name}")
    print("Done. Pushes to main that touch docs/ now deploy automatically; "
          "run the 'Deploy site' workflow once to go live now.")
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
