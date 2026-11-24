import pytest

from archgate.errors import RequirementsError
from archgate.requirements import Requirements


def test_valid_requirements_apply_defaults(make_req):
    req = make_req(features=["chat", "chat", "media"])
    assert req.regions == 1
    assert req.budget == "medium"
    assert req.features == ("chat", "media")


def test_collects_all_problems_at_once():
    with pytest.raises(RequirementsError) as exc:
        Requirements.from_dict({"name": "x", "availability": 120, "surprise": 1})
    problems = " ".join(exc.value.problems)
    assert "unknown fields: surprise" in problems
    assert "missing required field: daily_active_users" in problems
    assert "availability" in problems


@pytest.mark.parametrize(
    "field, value",
    [
        ("daily_active_users", True),
        ("daily_active_users", 10.5),
        ("daily_active_users", "lots"),
        ("consistency", "maybe"),
        ("features", ["Not Snake"]),
        ("regions", 0),
    ],
)
def test_rejects_bad_values(make_req, field, value):
    with pytest.raises(RequirementsError):
        make_req(**{field: value})


def test_round_trips_through_dict(make_req):
    req = make_req(features=["search"])
    assert Requirements.from_dict(req.to_dict()) == req
