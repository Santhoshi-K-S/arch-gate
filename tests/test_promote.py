import pytest

from archgate import promote
from archgate.errors import ArchGateError, KnowledgeBaseError
from archgate.knowledge import load_knowledge_base
from archgate.pipeline import Pipeline


def approved_run(kb, make_req, fake_llm, by="ana"):
    llm = fake_llm([
        {"add": [{"component": "event_stream", "reason": "collect user events"},
                 {"component": "ml_model_serving", "reason": "serve the model"}],
         "remove": [], "unresolved": []},
    ])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    run.decide(True, by=by)
    return run


def test_drafts_rule_from_approved_gap_fill(kb, make_req, fake_llm):
    (draft,) = promote.draft_rules(approved_run(kb, make_req, fake_llm), kb)
    assert draft["covers"] == ["loyalty_points"]
    assert draft["when"] == {"field": "features", "op": "contains", "value": "loyalty_points"}
    assert draft["then"]["add"] == ["ml_model_serving", "event_stream"]
    assert "approved by ana" in draft["why"]


def test_unapproved_run_cannot_be_promoted(kb, make_req, fake_llm):
    run = Pipeline(kb, llm=fake_llm([{"add": [], "remove": [], "unresolved": []}])).run(
        make_req(features=["loyalty_points"]))
    with pytest.raises(ArchGateError):
        promote.draft_rules(run, kb)


def test_promoted_rule_makes_next_run_deterministic(kb, make_req, fake_llm, tmp_path):
    path = tmp_path / "learned.yaml"
    promote.append_rules(path, promote.draft_rules(approved_run(kb, make_req, fake_llm), kb))

    learned_kb = load_knowledge_base(learned_path=path)
    assert any(r.origin == "learned" for r in learned_kb.rules)
    assert learned_kb.version != kb.version

    run = Pipeline(learned_kb).run(make_req(features=["loyalty_points"]))
    assert {"event_stream", "ml_model_serving"} <= run.design.components
    assert run.unresolved == []
    assert run.design.choices["event_stream"].deterministic


def test_invalid_draft_is_not_written(kb, tmp_path):
    path = tmp_path / "learned.yaml"
    bad = [{"id": "L001", "when": {"all": []}, "then": {"add": ["warp_drive"]}, "why": "x"}]
    with pytest.raises(KnowledgeBaseError, match="warp_drive"):
        promote.append_rules(path, bad)
    assert not path.exists()
