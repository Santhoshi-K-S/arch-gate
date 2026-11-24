import pytest

from archgate.estimator import estimate


def test_qps_and_storage(make_req):
    req = make_req(
        daily_active_users=8_640_000,
        requests_per_user_per_day=10,
        read_write_ratio=9,
        avg_write_kb=1,
        retention_years=1,
        peak_factor=2,
    )
    est = estimate(req)
    assert est.avg_qps == pytest.approx(1000)
    assert est.peak_qps == pytest.approx(2000)
    assert est.peak_write_qps == pytest.approx(200)
    assert est.peak_read_qps == pytest.approx(1800)
    # 8.64M writes/day * 1 KB * 365 days
    assert est.storage_tb == pytest.approx(8_640_000 * 365 / 1e9)
    assert est.replicated_storage_tb == pytest.approx(est.storage_tb * 3)
