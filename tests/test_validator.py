from archgate.design import Design
from archgate.estimator import estimate
from archgate.validator import validate

BASE = ["client", "load_balancer", "app_service", "postgres"]


def design_of(*ids):
    design = Design()
    for cid in ids:
        design.add(cid, "rule", "test", "test")
    return design


def checks(findings):
    return {f.check for f in findings}


def test_minimal_design_passes(kb, make_req):
    req = make_req()
    assert validate(design_of(*BASE), req, estimate(req), kb.catalog) == []


def test_missing_database_has_no_auto_fix(kb, make_req):
    req = make_req()
    findings = validate(design_of("client", "load_balancer", "app_service"), req,
                        estimate(req), kb.catalog)
    (finding,) = [f for f in findings if f.check == "V002"]
    assert not finding.has_auto_fix


def test_single_point_of_failure(kb, make_req):
    req = make_req(availability=99.9)
    findings = validate(design_of(*BASE), req, estimate(req), kb.catalog)
    assert "V005" in checks(findings)


def test_eventual_store_with_strong_consistency(kb, make_req):
    req = make_req(consistency="strong")
    design = design_of("client", "load_balancer", "app_service", "wide_column_store")
    findings = validate(design, req, estimate(req), kb.catalog)
    assert "V006" in checks(findings)


def test_payments_need_strong_consistency(kb, make_req):
    req = make_req(consistency="eventual", features=["payments"])
    findings = validate(design_of(*BASE), req, estimate(req), kb.catalog)
    (finding,) = [f for f in findings if f.check == "V007"]
    assert not finding.design_fixable


def test_unknown_and_duplicate_components(kb, make_req):
    req = make_req()
    design = design_of(*BASE, "sharded_sql", "flux_capacitor")
    assert {"V001", "V003"} <= checks(validate(design, req, estimate(req), kb.catalog))


def test_postgres_capacity_limit(kb, make_req):
    req = make_req(daily_active_users=50_000_000, avg_write_kb=50)
    findings = validate(design_of(*BASE), req, estimate(req), kb.catalog)
    (finding,) = [f for f in findings if f.check == "V008"]
    assert finding.fix_add == ("sharded_sql",)
