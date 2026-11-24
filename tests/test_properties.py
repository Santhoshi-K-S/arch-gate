"""Invariants that must hold for any input, checked on seeded random requirements."""

import pytest

from archgate.pipeline import Pipeline, State
from archgate.sampling import samples

REQS = samples(300, seed=11)


@pytest.fixture(scope="module")
def runs(kb):
    pipeline = Pipeline(kb)
    return [(req, pipeline.run(req)) for req in REQS]


def test_every_run_reaches_the_gate(runs):
    assert all(run.state is State.AWAITING_APPROVAL for _, run in runs)


def test_structure_is_always_valid(kb, runs):
    primary = set(kb.catalog.required_slots["primary_db"])
    for _, run in runs:
        comps = run.design.components
        assert len(comps & primary) == 1
        for slot, options in kb.catalog.required_slots.items():
            assert comps & set(options), slot
        for cid in comps:
            needs = kb.catalog.components[cid].requires_any
            assert not needs or comps & set(needs), cid
        for key in run.design.settings:
            assert comps & set(kb.catalog.settings[key].applies_to), key


def test_every_decision_has_a_reason(runs):
    for _, run in runs:
        assert all(choice.reasons for choice in run.design.choices.values())
        assert all(s.reason.why for s in run.design.settings.values())


def test_only_requirement_conflicts_can_block(runs):
    # with rules alone, anything fixable in the design must have been fixed
    for _, run in runs:
        assert all(not f.design_fixable for f in run.findings), run.findings


def test_runs_are_deterministic(kb, runs):
    pipeline = Pipeline(kb)
    for req, run in runs[:50]:
        again = pipeline.run(req)
        assert again.design.components == run.design.components
        assert {k: v.value for k, v in again.design.settings.items()} == {
            k: v.value for k, v in run.design.settings.items()
        }


def test_high_availability_never_has_a_single_database(runs):
    for req, run in runs:
        if req.availability >= 99.9 and "postgres" in run.design:
            assert "db_replicas" in run.design
