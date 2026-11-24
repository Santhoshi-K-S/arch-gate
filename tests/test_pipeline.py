import pytest

from archgate.errors import IllegalTransition, LLMError
from archgate.pipeline import Budget, Pipeline, State
from archgate.store import load_run, save_run


def test_rules_only_run_reaches_gate_with_high_confidence(kb, make_req):
    run = Pipeline(kb).run(make_req(availability=99.9))
    assert run.state is State.AWAITING_APPROVAL
    assert run.confidence == "high"
    assert run.findings == []
    assert all(c.deterministic for c in run.design.choices.values())


def test_uncovered_feature_without_model_needs_review(kb, make_req):
    run = Pipeline(kb).run(make_req(features=["loyalty_points"]))
    assert run.state is State.AWAITING_APPROVAL
    assert run.confidence == "review_needed"
    assert any("loyalty_points" in item for item in run.unresolved)


def test_model_fills_gap_and_is_marked(kb, make_req, fake_llm):
    llm = fake_llm([
        {"add": [{"component": "event_stream", "reason": "feed user events to a ranker"}],
         "remove": [], "unresolved": ["ranking model itself is out of scope"]},
    ])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    assert run.state is State.AWAITING_APPROVAL
    assert run.confidence == "review_needed"
    assert not run.design.choices["event_stream"].deterministic
    assert run.explanation
    actors = {e.actor for e in run.trace.events}
    assert {"rule", "llm", "validator", "code"} <= actors


def test_model_cannot_remove_rule_backed_components(kb, make_req, fake_llm):
    llm = fake_llm([
        {"add": [], "remove": [{"component": "postgres", "reason": "not needed"}],
         "unresolved": []},
    ])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    assert "postgres" in run.design
    assert any(e.action == "proposal_rejected" for e in run.trace.events)


def test_validator_auto_fixes_without_the_model(kb, make_req, fake_llm):
    # ~24 TB of eventual data: no rule picks a bigger store, the capacity check does
    llm = fake_llm([])
    run = Pipeline(kb, llm=llm).run(make_req(consistency="eventual", avg_write_kb=40))
    assert "wide_column_store" in run.design
    assert "postgres" not in run.design
    assert run.findings == []
    assert run.design.choices["wide_column_store"].deterministic
    assert llm.prompts == []


def test_unfixable_finding_escalates_as_blocked(kb, make_req):
    run = Pipeline(kb).run(make_req(consistency="eventual", features=["payments"]))
    assert run.state is State.AWAITING_APPROVAL
    assert run.confidence == "blocked"
    assert [f.check for f in run.findings] == ["V007"]
    assert any(e.action == "escalated" for e in run.trace.events)


def test_model_failure_degrades_gracefully(kb, make_req, fake_llm):
    llm = fake_llm([LLMError("connection refused")])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    assert run.state is State.AWAITING_APPROVAL
    assert any("model could not help" in u for u in run.unresolved)


def test_llm_call_budget_is_enforced(kb, make_req, fake_llm):
    llm = fake_llm([{"add": [], "remove": [], "unresolved": []}] * 10)
    run = Pipeline(kb, llm=llm, budget_factory=lambda: Budget(max_llm_calls=1)).run(
        make_req(features=["loyalty_points"])
    )
    assert run.llm_calls == 1
    assert any(e.action == "explain_failed" for e in run.trace.events)


def test_text_intake_retries_with_feedback(kb, fake_llm):
    good = {
        "name": "Chat", "daily_active_users": 1000, "requests_per_user_per_day": 10,
        "read_write_ratio": 2, "avg_write_kb": 1, "latency_p99_ms": 200,
        "availability": 99.5, "consistency": "eventual", "features": ["chat"],
    }
    bad = {**good, "availability": 999}
    llm = fake_llm([bad, good])
    run = Pipeline(kb, llm=llm).run("a small chat app")
    assert run.state is State.AWAITING_APPROVAL
    assert run.requirements_source == "llm"
    assert "availability" in llm.prompts[1]


def test_text_intake_without_model_fails(kb):
    run = Pipeline(kb).run("a chat app")
    assert run.state is State.FAILED


def test_rejected_confirmation_fails_run(kb, fake_llm):
    good = {
        "name": "Chat", "daily_active_users": 1000, "requests_per_user_per_day": 10,
        "read_write_ratio": 2, "avg_write_kb": 1, "latency_p99_ms": 200,
        "availability": 99.5, "consistency": "eventual", "features": [],
    }
    run = Pipeline(kb, llm=fake_llm([good]), confirm_requirements=lambda r: None).run("chat")
    assert run.state is State.FAILED


def test_gate_rules(kb, make_req):
    run = Pipeline(kb).run(make_req())
    with pytest.raises(ValueError):
        run.decide(False, by="ana")  # rejection needs a note
    run.decide(True, by="ana", note="lgtm")
    assert run.state is State.APPROVED
    with pytest.raises(IllegalTransition):
        run.decide(False, by="ana", note="changed my mind")


def test_blocked_design_needs_force(kb, make_req):
    run = Pipeline(kb).run(make_req(consistency="eventual", features=["payments"]))
    with pytest.raises(ValueError):
        run.decide(True, by="ana")
    run.decide(True, by="ana", note="accepted risk", force=True)
    assert run.decision["forced"]


def test_saved_run_round_trips_and_can_be_approved(kb, make_req, tmp_path):
    run = Pipeline(kb).run(make_req(features=["search", "media"]))
    run_dir = save_run(run, tmp_path, kb.catalog)
    for name in ("run.json", "trace.jsonl", "design.md", "design.mmd", "requirements.yaml"):
        assert (run_dir / name).exists()

    loaded = load_run(run_dir)
    assert loaded.design.components == run.design.components
    assert loaded.state is State.AWAITING_APPROVAL
    loaded.decide(True, by="ana")
    save_run(loaded, tmp_path, kb.catalog)
    assert load_run(run_dir).state is State.APPROVED


def test_contradictory_proposal_is_ignored(kb, make_req, fake_llm):
    llm = fake_llm([
        {"add": [{"component": "websocket_gateway", "reason": "live recs"}],
         "remove": [{"component": "websocket_gateway", "reason": "not needed"}],
         "unresolved": []},
    ])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    assert "websocket_gateway" not in run.design
    assert any(e.action == "proposal_contradiction" for e in run.trace.events)


def test_gap_filling_rejection_explains_why(kb, make_req, fake_llm):
    llm = fake_llm([
        {"add": [], "remove": [{"component": "cache", "reason": "x"}], "unresolved": []},
    ])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    (event,) = [e for e in run.trace.events if e.action == "proposal_rejected"]
    assert event.detail["why"] == "removals are not allowed while filling gaps"
