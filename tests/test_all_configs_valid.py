"""Every config*.yaml in the repo must parse and reference real providers
and a real use_case. This is a static check (no network) that catches the
class of typo that would otherwise only surface mid-run — e.g. a stray
model-id edit during a bulk find/replace across the config files."""
from __future__ import annotations

import pathlib

import pytest
import yaml

from eval_agents.registry import _PROVIDERS
from eval_agents.usecases import REGISTRY as USE_CASES

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FILES = sorted(ROOT.glob("config*.yaml"))


@pytest.mark.parametrize("path", CONFIG_FILES, ids=lambda p: p.name)
def test_config_parses_and_references_known_providers(path):
    config = yaml.safe_load(path.read_text())
    assert "candidates" in config, f"{path.name}: missing candidates"
    assert "judge" in config, f"{path.name}: missing judge"

    for spec in config["candidates"]:
        assert spec["provider"] in _PROVIDERS, (
            f"{path.name}: candidate {spec['name']!r} references unknown "
            f"provider {spec['provider']!r}"
        )
        assert spec.get("model"), f"{path.name}: candidate {spec['name']!r} has no model"

    judge_provider = config["judge"]["provider"]
    assert judge_provider in _PROVIDERS, f"{path.name}: judge references unknown provider {judge_provider!r}"

    use_case = config.get("use_case")
    if use_case is not None:
        assert use_case in USE_CASES, f"{path.name}: unknown use_case {use_case!r}"


@pytest.mark.parametrize("path", CONFIG_FILES, ids=lambda p: p.name)
def test_config_pricing_map_covers_priced_candidates(path):
    """If a scorecard.pricing map exists, every candidate name referenced in
    it must actually be a candidate in this config (catches stale entries
    left behind after a candidate is renamed/removed)."""
    config = yaml.safe_load(path.read_text())
    pricing = (config.get("scorecard") or {}).get("pricing")
    if not pricing:
        return
    candidate_names = {c["name"] for c in config["candidates"]}
    stale = set(pricing) - candidate_names
    assert not stale, f"{path.name}: pricing map has entries for non-candidates: {stale}"
