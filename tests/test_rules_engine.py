from pathlib import Path

import pytest

from archgate.errors import KnowledgeBaseError
from archgate.estimator import estimate
from archgate.knowledge import KnowledgeBase, load_catalog, load_rules
from archgate.rules_engine import RuleEngine, build_facts, coverage_gaps, evaluate


def run_rules(kb, req):
    return RuleEngine(kb).run(build_facts(req, estimate(req)))


@pytest.mark.parametrize(
    "cond, expected",
    [
        ({"field": "x", "op": ">", "value": 1}, True),
        ({"all": [{"field": "x", "op": ">", "value": 1}, {"field": "x", "op": "<", "value": 2}]},
         False),
        ({"any": [{"field": "x", "op": "==", "value": 0}, {"field": "x", "op": "==", "value": 5}]},
         True),
        ({"not": {"field": "tags", "op": "contains", "value": "a"}}, False),
        ({"field": "x", "op": "in", "value": [4, 5]}, True),
        ({"all": []}, True),
    ],
)
def test_evaluate(cond, expected):
    assert evaluate(cond, {"x": 5, "tags": ["a"]}) is expected


def test_small_app_gets_baseline_only(kb, make_req):
    outcome = run_rules(kb, make_req(daily_active_users=1000))
    assert outcome.design.components == {
        "client", "load_balancer", "app_service", "auth_service", "monitoring", "postgres",
        "backup_service",
    }


def test_exclusive_group_keeps_higher_priority_store(kb, make_req):
    req = make_req(daily_active_users=50_000_000, avg_write_kb=50, consistency="strong")
    outcome = run_rules(kb, req)
    assert "sharded_sql" in outcome.design
    assert "postgres" not in outcome.design
    # replicas only make sense with postgres, so they are dropped too
    assert "db_replicas" not in outcome.design
    kinds = {(r.kind, r.component) for r in outcome.resolutions}
    assert ("exclusive_group", "postgres") in kinds
    assert ("requires", "db_replicas") in kinds


def test_higher_priority_remove_beats_add(kb, make_req):
    # DM002 wants a cache for feeds (priority 40); CT002 removes it on a low budget (priority 50)
    req = make_req(daily_active_users=2000, budget="low", features=["feed"])
    outcome = run_rules(kb, req)
    assert "cache" not in outcome.design
    (res,) = [r for r in outcome.resolutions if r.component == "cache"]
    assert res.kind == "vote" and res.outcome == "removed"


def test_every_component_has_a_rule_reason(kb, make_req):
    outcome = run_rules(kb, make_req(features=["chat", "search"]))
    for choice in outcome.design.choices.values():
        assert choice.reasons and all(r.source == "rule" for r in choice.reasons)


def test_coverage_reports_unknown_features(kb, make_req):
    req = make_req(features=["search", "loyalty_points"])
    outcome = run_rules(kb, req)
    gaps = coverage_gaps(kb.catalog, outcome.design, req, outcome.covered_features)
    assert gaps == {"missing_slots": [], "uncovered_features": ["loyalty_points"]}


def test_rule_with_unknown_component_is_rejected(tmp_path: Path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - id: X1\n"
        "    when: {all: []}\n"
        "    then: {add: [quantum_db]}\n"
    )
    with pytest.raises(KnowledgeBaseError, match="quantum_db"):
        load_rules(load_catalog(), rules)


def test_rule_with_unknown_field_is_rejected(tmp_path: Path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - id: X1\n"
        "    when: {field: vibes, op: '==', value: good}\n"
        "    then: {add: [cache]}\n"
    )
    with pytest.raises(KnowledgeBaseError, match="vibes"):
        load_rules(load_catalog(), rules)


def test_bundled_knowledge_base_loads(kb: KnowledgeBase):
    assert len(kb.rules) >= 25
    assert "primary_db" in kb.catalog.groups
