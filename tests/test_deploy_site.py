"""scripts/deploy_site.py: idempotent Cloudflare Pages + Cloudflare DNS logic, exercised against a fake HTTP
layer (no network, no credentials)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAIN = "eval.aveto.dev"


def load_module(domain: str | None = DOMAIN):
    """A fresh copy of the script; it reads its domain and project at import (env, else release.yaml)."""
    saved = {k: os.environ.get(k) for k in ("SITE_DOMAIN", "CF_PAGES_PROJECT")}
    try:
        os.environ.pop("CF_PAGES_PROJECT", None)
        if domain is None:
            os.environ.pop("SITE_DOMAIN", None)
        else:
            os.environ["SITE_DOMAIN"] = domain
        spec = importlib.util.spec_from_file_location("deploy_site", ROOT / "scripts" / "deploy_site.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


ds = load_module()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in this module may reach a real host: `http` raises unless a test installs a fake."""
    def blocked(method, url, **kw):
        raise AssertionError(f"test tried to reach the network: {method} {url}")
    monkeypatch.setattr(ds, "http", blocked)


class FakeHTTP:
    """Scripted responses keyed by (method, url-suffix); records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers=None, data=None, timeout=30):
        self.calls.append((method, url, data))
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp(data) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url}")

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE")]


def cf_json(result, status=200):
    return status, json.dumps({"success": status < 300, "result": result})


ZONES = cf_json([{"id": "z-dev", "name": "aveto.dev"}, {"id": "z-k", "name": "kavachiq.com"}, {"id": "z-sub", "name": "eval.aveto.dev"}])
ZONE_LIST = "/zones?per_page=50"
RECORDS = "/zones/z-sub/dns_records?name=eval.aveto.dev"  # the longest matching zone wins
TARGET = "model-ledger.pages.dev"


def dns_routes(existing, extra=None):
    return {("GET", ZONE_LIST): ZONES, ("GET", RECORDS): cf_json(existing), **(extra or {})}


def rec(type_, content, proxied=True, id_="r1", name=DOMAIN):
    return {"id": id_, "type": type_, "name": name, "content": content, "proxied": proxied}


# ---------------------------------------------------------------- Cloudflare Pages


def test_configuration_comes_from_release_yaml_unless_overridden():
    fresh = load_module(domain=None)
    assert fresh.DOMAIN == "eval.aveto.dev" and fresh.PROJECT == "model-ledger"  # release.yaml `site:`
    assert load_module("eval.example.org").DOMAIN == "eval.example.org"


def test_project_created_only_when_missing():
    http = FakeHTTP({
        ("GET", f"/projects/{ds.PROJECT}"): (404, json.dumps({"success": False})),
        ("POST", "/pages/projects"): cf_json({"subdomain": TARGET}),
    })
    assert ds.Cloudflare("t", "acct", request=http).ensure_project() == TARGET
    assert [c[0] for c in http.calls] == ["GET", "POST"]
    assert json.loads(http.calls[1][2]) == {"name": ds.PROJECT, "production_branch": "main"}

    http = FakeHTTP({("GET", f"/projects/{ds.PROJECT}"): cf_json({"subdomain": "model-ledger-x1.pages.dev"})})
    assert ds.Cloudflare("t", "acct", request=http).ensure_project() == "model-ledger-x1.pages.dev"
    assert [c[0] for c in http.calls] == ["GET"]  # exists: no create


def test_domain_attached_only_when_missing_and_dry_run_is_read_only():
    http = FakeHTTP({("GET", f"/domains/{DOMAIN}"): (404, "{}")})
    ds.Cloudflare("t", "acct", request=http).ensure_domain(dry_run=True)
    assert [c[0] for c in http.calls] == ["GET"]

    http = FakeHTTP({
        ("GET", f"/domains/{DOMAIN}"): (404, "{}"),
        ("POST", "/domains"): cf_json({"name": DOMAIN, "status": "initializing"}),
    })
    ds.Cloudflare("t", "acct", request=http).ensure_domain()
    assert json.loads(http.calls[1][2]) == {"name": DOMAIN}


def test_cloudflare_errors_are_reported_not_swallowed():
    http = FakeHTTP({("GET", f"/projects/{ds.PROJECT}"): (403, json.dumps({"errors": [{"message": "no access"}]}))})
    with pytest.raises(ds.DeployError, match="403"):
        ds.Cloudflare("t", "acct", request=http).ensure_project()


# ---------------------------------------------------------------- Cloudflare DNS


@pytest.mark.parametrize("records,expected", [
    ([], "create"),
    ([rec("CNAME", TARGET)], "keep"),
    ([rec("CNAME", TARGET + ".")], "keep"),                       # trailing dot is the same name
    ([rec("CNAME", TARGET, proxied=False)], "update"),            # right target, not proxied: ours, fix it
    ([rec("CNAME", "old-project.pages.dev")], "update"),          # a previous Pages target: ours
    ([rec("CNAME", "somewhere.example.net")], "conflict"),        # someone else's record
    ([rec("A", "192.0.2.1")], "conflict"),
    ([rec("TXT", "v=spf1 -all")], "conflict"),
    ([rec("CNAME", TARGET), rec("TXT", "x", id_="r2")], "conflict"),
    ([rec("A", "192.0.2.1", name="other.aveto.dev")], "create"),  # a different name doesn't matter
])
def test_plan_dns_only_ever_changes_a_pages_cname(records, expected):
    assert ds.plan_dns(records, DOMAIN, TARGET)[0] == expected


def test_zone_is_found_by_longest_suffix_and_only_in_this_account():
    dns = ds.CloudflareDNS("t", request=FakeHTTP({("GET", ZONE_LIST): ZONES}))
    assert dns.find_zone("eval.aveto.dev")["id"] == "z-sub"
    assert dns.find_zone("docs.aveto.dev")["id"] == "z-dev"
    assert dns.find_zone("aveto.dev")["id"] == "z-dev"
    assert dns.find_zone("aveto.com") is None and dns.find_zone("notaveto.dev") is None  # suffix must be a label boundary
    with pytest.raises(ds.DeployError, match="list zones failed"):
        ds.CloudflareDNS("t", request=FakeHTTP({("GET", ZONE_LIST): (403, "{}")})).find_zone(DOMAIN)


def test_cname_created_proxied_when_missing():
    http = FakeHTTP(dns_routes([], {("POST", "/zones/z-sub/dns_records"): cf_json({"id": "new"})}))
    assert ds.CloudflareDNS("t", request=http).ensure_cname(DOMAIN, TARGET) == "create"
    (method, _, body), = http.writes()
    body = json.loads(body)
    assert method == "POST" and body["type"] == "CNAME" and body["name"] == DOMAIN and body["content"] == TARGET
    assert body["proxied"] is True and "scripts/deploy_site.py" in body["comment"]


def test_cname_updated_in_place_when_it_points_at_an_old_pages_target():
    http = FakeHTTP(dns_routes([rec("CNAME", "old-project.pages.dev", id_="r9")],
                               {("PATCH", "/zones/z-sub/dns_records/r9"): cf_json({"id": "r9"})}))
    assert ds.CloudflareDNS("t", request=http).ensure_cname(DOMAIN, TARGET) == "update"
    assert [c[0] for c in http.writes()] == ["PATCH"]


def test_correct_record_makes_no_write_and_dry_run_never_writes():
    http = FakeHTTP(dns_routes([rec("CNAME", TARGET)]))
    assert ds.CloudflareDNS("t", request=http).ensure_cname(DOMAIN, TARGET) == "keep" and not http.writes()
    http = FakeHTTP(dns_routes([]))
    assert ds.CloudflareDNS("t", request=http).ensure_cname(DOMAIN, TARGET, dry_run=True) == "create" and not http.writes()


def test_someone_elses_record_is_never_overwritten():
    http = FakeHTTP(dns_routes([rec("A", "192.0.2.1")]))
    with pytest.raises(ds.DeployError, match="already has a A record.*refusing to overwrite"):
        ds.CloudflareDNS("t", request=http).ensure_cname(DOMAIN, TARGET)
    assert not http.writes()


def test_domain_outside_the_account_is_reported_not_guessed():
    http = FakeHTTP({("GET", ZONE_LIST): cf_json([{"id": "z-k", "name": "kavachiq.com"}])})
    assert ds.CloudflareDNS("t", request=http).ensure_cname(DOMAIN, TARGET) == "no-zone" and not http.writes()


def test_missing_dns_permission_says_which_permission_to_add():
    with pytest.raises(ds.DeployError, match="Zone > DNS > Edit"):
        ds.CloudflareDNS("t", request=FakeHTTP(dns_routes([], {("POST", "/zones/z-sub/dns_records"): (403, "{}")}))).ensure_cname(DOMAIN, TARGET)
    with pytest.raises(ds.DeployError, match="Zone > DNS > Edit"):
        ds.CloudflareDNS("t", request=FakeHTTP({("GET", ZONE_LIST): ZONES, ("GET", RECORDS): (403, "{}")})).ensure_cname(DOMAIN, TARGET)


# ---------------------------------------------------------------- deploy flow


def _deploy_env(monkeypatch, routes, *, domain=DOMAIN):
    monkeypatch.setattr(ds, "DOMAIN", domain)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    http = FakeHTTP(routes)
    monkeypatch.setattr(ds, "http", http)
    seen = []
    monkeypatch.setattr(ds, "upload_site", lambda dry_run=False: seen.append("upload"))
    monkeypatch.setattr(ds, "ensure_indexnow_key_file", lambda *a, **k: seen.append("key-file"))
    monkeypatch.setattr(ds, "verify", lambda cf, url=None, check_domain=True, **k: seen.append(("verify", url, check_domain)) or True)
    monkeypatch.setattr(ds, "ping_indexnow", lambda urls, *a, **k: seen.append(("ping", urls)))
    return http, seen


def test_full_deploy_attaches_the_domain_before_its_dns_and_notifies_search_engines(monkeypatch):
    http, seen = _deploy_env(monkeypatch, {
        ("GET", f"/projects/{ds.PROJECT}"): cf_json({"subdomain": TARGET}),
        ("GET", f"/domains/{DOMAIN}"): (404, "{}"),
        ("POST", "/domains"): cf_json({"name": DOMAIN, "status": "initializing"}),
        ("GET", ZONE_LIST): ZONES, ("GET", RECORDS): cf_json([]),
        ("POST", "/zones/z-sub/dns_records"): cf_json({"id": "new"}),
    })
    assert ds.main(["deploy"]) == 0
    assert seen == ["key-file", "upload", ("verify", f"https://{DOMAIN}/", True), ("ping", [f"https://{DOMAIN}/"])]
    order = [(m, u.rsplit("/", 1)[-1].split("?")[0]) for m, u, _ in http.calls]
    assert order.index(("POST", "domains")) < order.index(("POST", "dns_records"))  # domain first, or Cloudflare 522s the hostname


def test_domain_outside_the_account_still_publishes_at_pages_dev(monkeypatch):
    http, seen = _deploy_env(monkeypatch, {
        ("GET", f"/projects/{ds.PROJECT}"): cf_json({"subdomain": TARGET}),
        ("GET", f"/domains/{DOMAIN}"): cf_json({"name": DOMAIN, "status": "pending"}),
        ("GET", ZONE_LIST): cf_json([{"id": "z-k", "name": "kavachiq.com"}]),
    })
    assert ds.main(["deploy"]) == 0
    assert seen == ["key-file", "upload", ("verify", f"https://{TARGET}/", False)]  # no ping: the domain isn't live


def test_deploy_without_a_configured_domain_touches_no_domain_or_dns(monkeypatch):
    http, seen = _deploy_env(monkeypatch, {("GET", f"/projects/{ds.PROJECT}"): cf_json({"subdomain": TARGET})}, domain="")
    assert ds.main(["deploy"]) == 0
    assert seen == ["upload", ("verify", f"https://{TARGET}/", False)]  # no key file, no domain, no DNS, no IndexNow


def test_dns_conflict_fails_the_deploy_visibly(monkeypatch):
    _deploy_env(monkeypatch, {
        ("GET", f"/projects/{ds.PROJECT}"): cf_json({"subdomain": TARGET}),
        ("GET", f"/domains/{DOMAIN}"): cf_json({"name": DOMAIN, "status": "pending"}),
        ("GET", ZONE_LIST): ZONES, ("GET", RECORDS): cf_json([rec("A", "192.0.2.1")]),
    })
    assert ds.main(["deploy"]) == 1


def test_missing_credentials_point_to_doctor(monkeypatch):
    for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert ds.main(["deploy", "--dry-run"]) == 1


# ---------------------------------------------------------------- IndexNow


def test_indexnow_key_is_deterministic_per_domain_and_stale_key_files_are_removed(tmp_path):
    key = ds.indexnow_key("eval.aveto.dev")
    assert key == ds.indexnow_key("eval.aveto.dev") and len(key) == 32 and key != ds.indexnow_key("eval.aveto.com")
    old = tmp_path / f"{ds.indexnow_key('eval.aveto.com')}.txt"
    old.write_text("old domain's key")
    (tmp_path / "index.html").write_text("keep me")
    path = ds.ensure_indexnow_key_file(tmp_path, "eval.aveto.dev")
    assert path.name == f"{key}.txt" and path.read_text().strip() == key
    assert not old.exists() and (tmp_path / "index.html").exists()  # only key files are cleaned up
    path.write_text("tampered")
    assert ds.ensure_indexnow_key_file(tmp_path, "eval.aveto.dev").read_text().strip() == key


def test_indexnow_ping_sends_host_key_and_location():
    http = FakeHTTP({("POST", "/indexnow"): (200, "")})
    assert ds.ping_indexnow([f"https://{DOMAIN}/"], DOMAIN, request=http) is True
    body = json.loads(http.calls[0][2])
    assert body["host"] == DOMAIN and body["urlList"] == [f"https://{DOMAIN}/"]
    assert body["keyLocation"] == f"https://{DOMAIN}/{ds.indexnow_key(DOMAIN)}.txt"
    assert ds.ping_indexnow(["u"], DOMAIN, request=FakeHTTP({("POST", "/indexnow"): (403, "")})) is False


# ---------------------------------------------------------------- transport


def test_every_request_identifies_itself_because_cloudflare_blocks_the_default_user_agent(monkeypatch):
    """Cloudflare answers Python's default User-Agent with HTTP 403 / error 1010, on the sites it serves."""
    import urllib.request

    seen = {}

    class Resp:
        status = 200

        def read(self, n=-1):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return Resp()

    monkeypatch.undo()  # this test exercises the real `http`, which the autouse fixture replaced
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ds.http("GET", "https://example.com/", headers={"Authorization": "Bearer x"})[0] == 200
    assert seen["user-agent"].startswith("multi-agent-eval-deploy/") and seen["authorization"] == "Bearer x"
