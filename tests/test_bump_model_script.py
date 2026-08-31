"""Tests for scripts/bump_model.py's pure logic against a throwaway
temp copy of a real config — never touches the actual repo files."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "bump_model.py"
spec = importlib.util.spec_from_file_location("bump_model", SCRIPT)
bump_model_mod = importlib.util.module_from_spec(spec)
sys.modules["bump_model"] = bump_model_mod
spec.loader.exec_module(bump_model_mod)


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    (tmp_path / "eval_agents" / "providers").mkdir(parents=True)
    (tmp_path / "config.a.yaml").write_text("candidates:\n  - model: glm-5.2\n")
    (tmp_path / "config.b.yaml").write_text(
        "candidates:\n  - model: glm-5.2\nscorecard:\n  pricing:\n    glm: [1.40, 4.40]  # glm-5.2\n"
    )
    (tmp_path / "config.unrelated.yaml").write_text("candidates:\n  - model: gpt-5.6-sol\n")
    (tmp_path / "eval_agents" / "providers" / "zai_provider.py").write_text(
        'model: str = "glm-5.2"\n'
    )
    monkeypatch.setattr(bump_model_mod, "ROOT", tmp_path)
    return tmp_path


def test_bump_model_updates_only_matching_files(fake_repo):
    changed = bump_model_mod.bump_model("glm-5.2", "glm-5.3")
    changed_names = {p.name for p in changed}
    assert changed_names == {"config.a.yaml", "config.b.yaml", "zai_provider.py"}
    assert "gpt-5.6-sol" in (fake_repo / "config.unrelated.yaml").read_text()  # untouched
    assert "glm-5.3" in (fake_repo / "config.a.yaml").read_text()
    assert "glm-5.2" not in (fake_repo / "config.a.yaml").read_text()


def test_bump_model_no_match_returns_empty(fake_repo):
    assert bump_model_mod.bump_model("not-a-real-model", "x") == []


def test_bump_price_updates_pricing_line_preserves_comment(fake_repo):
    changed = bump_model_mod.bump_price("glm", 2.00, 5.00)
    assert len(changed) == 1
    text = (fake_repo / "config.b.yaml").read_text()
    assert "glm: [2.00, 5.00]  # glm-5.2" in text


def test_bump_price_no_pricing_map_is_a_noop(fake_repo):
    assert bump_model_mod.bump_price("nonexistent_key", 1.0, 2.0) == []
