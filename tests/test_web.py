import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from archgate.web.app import WebConfig, create_app  # noqa: E402
from archgate.web.storage import normalize_url  # noqa: E402

FORM = {
    "name": "Shop",
    "daily_active_users": "500,000",
    "requests_per_user_per_day": "40",
    "read_write_ratio": "20",
    "avg_write_kb": "4",
    "latency_p99_ms": "200",
    "availability": "99.95",
    "consistency": "strong",
    "features": "search, payments",
}


@pytest.fixture
def client(tmp_path):
    app = create_app(WebConfig(database_url=f"sqlite:///{tmp_path / 'test.db'}"))
    return TestClient(app)


def create(client, data):
    return client.post("/designs", data=data, follow_redirects=False)


def test_pages_load(client):
    assert client.get("/healthz").text == "ok"
    assert client.get("/").status_code == 200
    assert "Generate design" in client.get("/new").text


def test_form_creates_a_rules_only_design(client):
    resp = create(client, FORM)
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    assert "Shop" in page.text
    assert "payment_gateway" in page.text
    assert "Download PNG" in page.text and "Download SVG" in page.text
    assert "Shop" in client.get("/").text


def test_compliance_and_flags_from_checkboxes(client):
    data = {**FORM, "hot_keys": "on"}
    resp = client.post("/designs", data={**data, "compliance": ["pci"]}, follow_redirects=False)
    page = client.get(resp.headers["location"]).text
    assert "tokenization_vault" in page


def test_yaml_takes_precedence(client):
    yaml_text = (
        "name: From YAML\ndaily_active_users: 1000\nrequests_per_user_per_day: 10\n"
        "read_write_ratio: 2\navg_write_kb: 1\nlatency_p99_ms: 300\navailability: 99.5\n"
        "consistency: eventual\n"
    )
    resp = create(client, {"yaml": yaml_text})
    assert "From YAML" in client.get(resp.headers["location"]).text


def test_invalid_input_shows_errors(client):
    resp = create(client, {**FORM, "availability": "120", "latency_p99_ms": "fast"})
    assert resp.status_code == 400
    assert "availability" in resp.text and "not a number" in resp.text


def test_downloads(client):
    run_url = create(client, FORM).headers["location"]
    for suffix, marker in (("diagram.mmd", "flowchart LR"), ("design.md", "# Shop"),
                           ("run.json", '"run_id"')):
        resp = client.get(f"{run_url}/{suffix}")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        assert marker in resp.text


def test_web_never_calls_a_model(client):
    run_url = create(client, FORM).headers["location"]
    assert '"llm_calls": 0' in client.get(f"{run_url}/run.json").text


def test_unknown_run_is_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_render_postgres_urls_get_a_driver():
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"
