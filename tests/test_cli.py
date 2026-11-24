from archgate import cli
from archgate.pipeline import Pipeline, State


def scripted_input(monkeypatch, answers):
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))


def test_approve_note_is_optional(kb, make_req, monkeypatch, tmp_path):
    run = Pipeline(kb).run(make_req())
    scripted_input(monkeypatch, ["a", ""])
    cli._interactive_gate(run, tmp_path / run.run_id, kb.catalog)
    assert run.state is State.APPROVED
    assert run.decision["note"] == ""


def test_reject_keeps_asking_for_a_reason(kb, make_req, monkeypatch, tmp_path):
    run = Pipeline(kb).run(make_req())
    scripted_input(monkeypatch, ["r", "", "  ", "too expensive"])
    cli._interactive_gate(run, tmp_path / run.run_id, kb.catalog)
    assert run.state is State.REJECTED
    assert run.decision["note"] == "too expensive"


def test_summary_lists_model_suggestions(kb, make_req, fake_llm, capsys, tmp_path):
    llm = fake_llm([
        {"add": [{"component": "event_stream", "reason": "collect user events"}],
         "remove": [], "unresolved": []},
    ])
    run = Pipeline(kb, llm=llm).run(make_req(features=["loyalty_points"]))
    cli._print_summary(run, tmp_path)
    out = capsys.readouterr().out
    assert "model suggestions" in out
    assert "+ event_stream: collect user events" in out
