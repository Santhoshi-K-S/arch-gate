"""Seeded random requirements, used by property tests and the rule audit."""

from __future__ import annotations

import random

from archgate.requirements import (
    BUDGET_LEVELS,
    COMPLIANCE_REGIMES,
    CONSISTENCY_LEVELS,
    DATA_MODELS,
    ORDERING_LEVELS,
    TEAM_SIZES,
    TRAFFIC_PATTERNS,
    WRITE_PATTERNS,
    Requirements,
)

KNOWN_FEATURES = (
    "chat", "realtime", "feed", "media", "file_upload", "search", "payments", "geo",
    "notifications", "analytics", "batch_jobs", "public_api", "recommendations",
    "semantic_search", "short_links", "collaboration",
)


def random_requirements(rng: random.Random, name: str = "sample") -> Requirements:
    return Requirements.from_dict({
        "name": name,
        "daily_active_users": rng.choice([100, 5_000, 50_000, 500_000, 5_000_000, 50_000_000]),
        "requests_per_user_per_day": rng.choice([1, 10, 50, 200, 2000]),
        "read_write_ratio": rng.choice([0.1, 1, 3, 10, 50, 500]),
        "avg_write_kb": rng.choice([0.2, 1, 10, 200, 20_000]),
        "latency_p99_ms": rng.choice([30, 100, 300, 2000]),
        "availability": rng.choice([99.0, 99.5, 99.9, 99.95, 99.99]),
        "consistency": rng.choice(CONSISTENCY_LEVELS),
        "retention_years": rng.choice([1, 5, 10]),
        "regions": rng.choice([1, 1, 1, 2, 3]),
        "budget": rng.choice(BUDGET_LEVELS),
        "features": rng.sample(KNOWN_FEATURES, k=rng.randint(0, 4)),
        "data_model": rng.choice(DATA_MODELS),
        "write_pattern": rng.choice(WRITE_PATTERNS),
        "traffic_pattern": rng.choice(TRAFFIC_PATTERNS),
        "ordering": rng.choice(ORDERING_LEVELS),
        "hot_keys": rng.random() < 0.3,
        "multi_tenant": rng.random() < 0.3,
        "compliance": rng.sample(COMPLIANCE_REGIMES, k=rng.choice([0, 0, 0, 1, 2])),
        "team_size": rng.choice(TEAM_SIZES),
        "rpo_minutes": rng.choice([0, 5, 60, 1440]),
        "rto_minutes": rng.choice([5, 30, 240, 1440]),
    })


def samples(count: int, seed: int = 7) -> list[Requirements]:
    rng = random.Random(seed)
    return [random_requirements(rng, f"sample-{i}") for i in range(count)]
