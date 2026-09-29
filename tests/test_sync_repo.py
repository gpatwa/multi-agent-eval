"""scripts/sync_repo.py: idempotent repo metadata sync that never links a dead homepage."""
from __future__ import annotations

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sync_repo", ROOT / "scripts" / "sync_repo.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

DESIRED = {"description": "A harness", "homepage": "https://eval.aveto.com", "topics": ["LLM_Evaluation", "ai evals", "llm-evaluation"]}


class FakeGh:
    def __init__(self, description="", homepage="", topics=()):
        self.state = {"nameWithOwner": "o/r", "description": description, "homepageUrl": homepage,
                      "repositoryTopics": [{"name": t} for t in topics]}
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ["repo", "view"]:
            return 0, json.dumps(self.state)
        return 0, ""


def test_topics_are_normalized_deduped_and_capped():
    assert sr.normalize_topics(DESIRED["topics"]) == ["llm-evaluation", "ai-evals"]
    assert len(sr.normalize_topics([f"t{i}" for i in range(30)])) == 20


def test_writes_only_what_differs_and_sets_topics_via_put():
    gh = FakeGh(description="old", topics=["ai-evals", "llm-evaluation"])
    assert sr.sync(DESIRED, gh_fn=gh, live_fn=lambda u: True, log=lambda m: None) == ["description", "homepage"]
    patch = next(c for c in gh.calls if c[0][:3] == ["api", "-X", "PATCH"])
    assert "description=A harness" in patch[0] and "homepage=https://eval.aveto.com" in patch[0]
    assert not any(c[0][:3] == ["api", "-X", "PUT"] for c in gh.calls)  # topics already matched


def test_homepage_is_skipped_until_the_site_is_live():
    gh = FakeGh(description="A harness", topics=["ai-evals", "llm-evaluation"])
    logs = []
    assert sr.sync(DESIRED, gh_fn=gh, live_fn=lambda u: False, log=logs.append) == []
    assert any("isn't serving the landing page yet" in m for m in logs) and len(gh.calls) == 1


def test_in_sync_makes_no_writes_and_dry_run_makes_none_either():
    gh = FakeGh("A harness", "https://eval.aveto.com/", ["ai-evals", "llm-evaluation"])
    assert sr.sync(DESIRED, gh_fn=gh, live_fn=lambda u: True) == [] and len(gh.calls) == 1
    gh2 = FakeGh()
    assert set(sr.sync(DESIRED, gh_fn=gh2, live_fn=lambda u: True, dry_run=True)) == {"description", "homepage", "topics"}
    assert len(gh2.calls) == 1  # only the read
    gh3 = FakeGh()
    sr.sync(DESIRED, gh_fn=gh3, live_fn=lambda u: True)
    put = next(c for c in gh3.calls if c[0][:3] == ["api", "-X", "PUT"])
    assert json.loads(put[1]) == {"names": ["llm-evaluation", "ai-evals"]}


def test_site_check_sends_a_real_user_agent(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def read(self, n=-1):
            return b"<title>The Model Ledger</title>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return Resp()

    monkeypatch.setattr(sr.urllib.request, "urlopen", fake_urlopen)
    assert sr.site_is_live("https://example.com/") is True
    assert seen["user-agent"].startswith("multi-agent-eval-sync/")
