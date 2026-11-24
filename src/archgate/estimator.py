from __future__ import annotations

from dataclasses import asdict, dataclass

from archgate.requirements import Requirements

SECONDS_PER_DAY = 86_400
DAYS_PER_YEAR = 365
KB_PER_TB = 1_000_000_000
REPLICATION_FACTOR = 3


@dataclass(frozen=True)
class Estimate:
    avg_qps: float
    peak_qps: float
    peak_read_qps: float
    peak_write_qps: float
    storage_tb: float
    replicated_storage_tb: float
    peak_egress_mbps: float

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in asdict(self).items()}


def estimate(req: Requirements) -> Estimate:
    """Back-of-envelope capacity numbers. Deterministic, so the model never invents them."""
    daily_requests = req.daily_active_users * req.requests_per_user_per_day
    write_share = 1 / (req.read_write_ratio + 1)

    avg_qps = daily_requests / SECONDS_PER_DAY
    peak_qps = avg_qps * req.peak_factor
    peak_write_qps = peak_qps * write_share
    peak_read_qps = peak_qps - peak_write_qps

    writes_per_day = daily_requests * write_share
    storage_tb = (
        writes_per_day * req.avg_write_kb * DAYS_PER_YEAR * req.retention_years / KB_PER_TB
    )

    # assume each read returns roughly one stored object
    peak_egress_mbps = peak_read_qps * req.avg_write_kb * 8 / 1000

    return Estimate(
        avg_qps=avg_qps,
        peak_qps=peak_qps,
        peak_read_qps=peak_read_qps,
        peak_write_qps=peak_write_qps,
        storage_tb=storage_tb,
        replicated_storage_tb=storage_tb * REPLICATION_FACTOR,
        peak_egress_mbps=peak_egress_mbps,
    )
