"""scripts/deploy_site.py: idempotent Cloudflare Pages + NIC.RU DNS logic,
exercised against a fake HTTP layer (no network, no credentials)."""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deploy_site", ROOT / "scripts" / "deploy_site.py")
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)


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


def cf_json(result, status=200):
    return status, json.dumps({"success": status < 300, "result": result})


# ---------------------------------------------------------------- Cloudflare


def test_project_created_only_when_missing():
    http = FakeHTTP({
        ("GET", f"/projects/{ds.PROJECT}"): (404, json.dumps({"success": False})),
        ("POST", "/pages/projects"): cf_json({"subdomain": "model-ledger.pages.dev"}),
    })
    assert ds.Cloudflare("t", "acct", request=http).ensure_project() == "model-ledger.pages.dev"
    assert [c[0] for c in http.calls] == ["GET", "POST"]
    assert json.loads(http.calls[1][2]) == {"name": ds.PROJECT, "production_branch": "main"}

    http = FakeHTTP({("GET", f"/projects/{ds.PROJECT}"): cf_json({"subdomain": "model-ledger-x1.pages.dev"})})
    assert ds.Cloudflare("t", "acct", request=http).ensure_project() == "model-ledger-x1.pages.dev"
    assert [c[0] for c in http.calls] == ["GET"]  # exists: no create


def test_domain_attached_only_when_missing_and_dry_run_is_read_only():
    http = FakeHTTP({("GET", f"/domains/{ds.DOMAIN}"): (404, "{}")})
    ds.Cloudflare("t", "acct", request=http).ensure_domain(dry_run=True)
    assert [c[0] for c in http.calls] == ["GET"]

    http = FakeHTTP({
        ("GET", f"/domains/{ds.DOMAIN}"): (404, "{}"),
        ("POST", "/domains"): cf_json({"name": ds.DOMAIN, "status": "initializing"}),
    })
    ds.Cloudflare("t", "acct", request=http).ensure_domain()
    assert json.loads(http.calls[1][2]) == {"name": ds.DOMAIN}


def test_cloudflare_errors_are_reported_not_swallowed():
    http = FakeHTTP({("GET", f"/projects/{ds.PROJECT}"): (403, json.dumps({"errors": [{"message": "no access"}]}))})
    with pytest.raises(ds.DeployError, match="403"):
        ds.Cloudflare("t", "acct", request=http).ensure_project()


# ---------------------------------------------------------------- NIC.RU DNS


def test_cname_label():
    assert ds.cname_label("eval.aveto.com", "aveto.com") == "eval"
    assert ds.cname_label("a.b.aveto.com", "aveto.com") == "a.b"
    with pytest.raises(ds.DeployError):
        ds.cname_label("aveto.com", "aveto.com")
    with pytest.raises(ds.DeployError):
        ds.cname_label("eval.other.com", "aveto.com")


def test_cname_xml_uses_fqdn_target_and_roundtrips():
    xml = ds.cname_xml("eval", "model-ledger.pages.dev")
    assert "<name>model-ledger.pages.dev.</name>" in xml  # trailing dot: not relative to the zone
    records = ds.parse_records(xml.replace("<rr>", '<rr id="7">'))
    assert records == [{"id": "7", "name": "eval", "type": "CNAME", "target": "model-ledger.pages.dev"}]


RECORDS = """<?xml version="1.0" encoding="UTF-8" ?><response><status>success</status><data>
<zone admin="1/NIC-D" id="1" name="aveto.com" service="SVC">
<rr id="10"><name>@</name><type>A</type><a>192.0.2.1</a></rr>
{extra}
</zone></data></response>"""


@pytest.mark.parametrize("extra,expected", [
    ("", {"keep": False, "delete": [], "add": True}),
    ('<rr id="11"><name>eval</name><type>CNAME</type><cname><name>model-ledger.pages.dev.</name></cname></rr>',
     {"keep": True, "delete": [], "add": False}),
    ('<rr id="12"><name>eval</name><type>CNAME</type><cname><name>old.example.net.</name></cname></rr>',
     {"keep": False, "delete": ["12"], "add": True}),
    ('<rr id="13"><name>eval</name><type>A</type><a>192.0.2.9</a></rr>',
     {"keep": False, "delete": ["13"], "add": True}),
])
def test_plan_dns(extra, expected):
    records = ds.parse_records(RECORDS.format(extra=extra))
    assert ds.plan_dns(records, "eval", "model-ledger.pages.dev") == expected


SERVICES = '<response><status>success</status><data><service name="SVC" /></data></response>'
ZONES = '<response><status>success</status><data><zone name="aveto.com" service="SVC" /></data></response>'


def _nic(records_extra):
    http = FakeHTTP({
        ("POST", "/oauth/token"): (200, json.dumps({"access_token": "tok"})),
        ("GET", "/dns-master/services"): (200, SERVICES),
        ("GET", "/services/SVC/zones"): (200, ZONES),
        ("GET", "/zones/aveto.com/records"): (200, RECORDS.format(extra=records_extra)),
        ("DELETE", "/records/12"): (200, "<response><status>success</status></response>"),
        ("PUT", "/zones/aveto.com/records"): (200, "<response><status>success</status></response>"),
        ("POST", "/zones/aveto.com/commit"): (200, "<response><status>success</status></response>"),
    })
    return ds.NicRu("u", "p", "cid", "sec", request=http), http


def test_nic_replaces_stale_cname_then_commits():
    nic, http = _nic('<rr id="12"><name>eval</name><type>CNAME</type><cname><name>old.example.net.</name></cname></rr>')
    nic.ensure_cname("aveto.com", "eval", "model-ledger.pages.dev")
    methods = [(c[0], c[1].rsplit("/", 1)[-1]) for c in http.calls[4:]]
    assert methods == [("DELETE", "12"), ("PUT", "records"), ("POST", "commit")]
    token_body = http.calls[0][2].decode()
    assert "grant_type=password" in token_body and "scope=.%2B%3A%2Fdns-master%2F.%2B" in token_body


def test_nic_is_a_no_op_when_record_is_correct():
    nic, http = _nic('<rr id="11"><name>eval</name><type>CNAME</type><cname><name>model-ledger.pages.dev.</name></cname></rr>')
    nic.ensure_cname("aveto.com", "eval", "model-ledger.pages.dev")
    assert not any(c[0] in ("PUT", "DELETE") or c[1].endswith("commit") for c in http.calls)


def test_nic_dry_run_changes_nothing():
    nic, http = _nic("")
    nic.ensure_cname("aveto.com", "eval", "model-ledger.pages.dev", dry_run=True)
    assert not any(c[0] in ("PUT", "DELETE") or c[1].endswith("commit") for c in http.calls)


def test_nic_bad_credentials_fail_clearly():
    http = FakeHTTP({("POST", "/oauth/token"): (401, '{"error": "invalid_grant"}')})
    with pytest.raises(ds.DeployError, match="NIC.RU OAuth failed"):
        ds.NicRu("u", "bad", "cid", "sec", request=http)


def test_missing_credentials_point_to_bootstrap(monkeypatch):
    for name, *_ in ds.SECRETS:
        monkeypatch.delenv(name, raising=False)
    assert ds.main(["deploy", "--dry-run"]) == 1
