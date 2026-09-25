"""Static checks on the triage task suites: every gold label must be a
value the scorer can actually match, so a typo in a ticket never silently
turns into a guaranteed miss for every model. Covers the public suite and,
when present locally, the gitignored held-out suite."""
from __future__ import annotations

import pathlib

import pytest

from eval_agents.config import load_tasks
from eval_agents.usecases.triage import ACTIONS, CATEGORIES, PRIORITIES

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "tasks.triage.yaml"
PRIVATE = ROOT / "tasks.triage.private.yaml"
SUITES = [PUBLIC] + ([PRIVATE] if PRIVATE.exists() else [])


@pytest.mark.parametrize("path", SUITES, ids=lambda p: p.name)
def test_gold_labels_are_valid(path):
    tasks = load_tasks(path)
    ids = [t.id for t in tasks]
    assert len(ids) == len(set(ids)), f"{path.name}: duplicate task ids"
    for t in tasks:
        assert t.gold.get("category") in CATEGORIES, f"{t.id}: bad category {t.gold.get('category')!r}"
        assert t.gold.get("priority") in PRIORITIES, f"{t.id}: bad priority {t.gold.get('priority')!r}"
        assert t.reference.strip(), f"{t.id}: missing reference for the judge"
        for key, value in (t.gold.get("actions") or {}).items():
            assert key in ACTIONS, f"{t.id}: unknown action {key!r}"
            assert value in ACTIONS[key], f"{t.id}: bad {key} value {value!r}"


def test_public_suite_covers_every_category_and_priority():
    tasks = load_tasks(PUBLIC)
    assert {t.gold["category"] for t in tasks} == set(CATEGORIES)
    assert {t.gold["priority"] for t in tasks} == set(PRIORITIES)


@pytest.mark.skipif(not PRIVATE.exists(), reason="held-out suite not present (gitignored)")
def test_held_out_suite_does_not_overlap_public():
    public, private = load_tasks(PUBLIC), load_tasks(PRIVATE)
    assert not {t.id for t in public} & {t.id for t in private}
    assert not {t.prompt.strip() for t in public} & {t.prompt.strip() for t in private}
