from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import MISSING, asdict, dataclass, fields
from typing import Any

from archgate.errors import RequirementsError

CONSISTENCY_LEVELS = ("strong", "eventual")
BUDGET_LEVELS = ("low", "medium", "high")
DATA_MODELS = ("relational", "document", "key_value", "graph", "time_series")
WRITE_PATTERNS = ("balanced", "append_only", "update_heavy")
TRAFFIC_PATTERNS = ("steady", "spiky", "seasonal")
ORDERING_LEVELS = ("none", "per_key", "global")
TEAM_SIZES = ("small", "medium", "large")
COMPLIANCE_REGIMES = ("pci", "hipaa", "gdpr", "soc2")
_FEATURE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Requirements:
    name: str
    daily_active_users: int
    requests_per_user_per_day: float
    read_write_ratio: float
    avg_write_kb: float
    latency_p99_ms: int
    availability: float
    consistency: str
    retention_years: float = 5.0
    peak_factor: float = 3.0
    regions: int = 1
    budget: str = "medium"
    features: tuple[str, ...] = ()
    description: str = ""
    data_model: str = "relational"
    write_pattern: str = "balanced"
    traffic_pattern: str = "steady"
    ordering: str = "none"
    hot_keys: bool = False
    multi_tenant: bool = False
    compliance: tuple[str, ...] = ()
    team_size: str = "medium"
    rpo_minutes: int = 60
    rto_minutes: int = 240

    @classmethod
    def from_dict(cls, data: Any) -> Requirements:
        if not isinstance(data, dict):
            raise RequirementsError(["requirements must be a mapping"])

        problems: list[str] = []
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            problems.append(f"unknown fields: {', '.join(unknown)}")

        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                if f.default is MISSING:
                    problems.append(f"missing required field: {f.name}")
                continue
            try:
                values[f.name] = _PARSERS[f.name](data[f.name])
            except ValueError as exc:
                problems.append(f"{f.name}: {exc}")

        if problems:
            raise RequirementsError(problems)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["features"] = list(self.features)
        out["compliance"] = list(self.compliance)
        return out


def _number(kind: type, check: Callable[[float], bool], rule: str) -> Callable[[Any], Any]:
    def parse(value: Any) -> Any:
        # bool is a subclass of int, so reject it explicitly
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"expected a number, got {value!r}")
        if kind is int:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(f"expected a whole number, got {value!r}")
            value = int(value)
        else:
            value = float(value)
        if not check(value):
            raise ValueError(f"{rule}, got {value!r}")
        return value

    return parse


def _choice(options: tuple[str, ...]) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        if not isinstance(value, str) or value.lower() not in options:
            raise ValueError(f"must be one of {', '.join(options)}, got {value!r}")
        return value.lower()

    return parse


def _text(required: bool) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(f"expected text, got {value!r}")
        if required and not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    return parse


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected true or false, got {value!r}")
    return value


def _regimes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected a list")
    unknown = [v for v in value if v not in COMPLIANCE_REGIMES]
    if unknown:
        raise ValueError(f"unknown regimes {unknown}; known: {', '.join(COMPLIANCE_REGIMES)}")
    return tuple(dict.fromkeys(value))


def _features(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected a list of feature tags")
    tags = []
    for item in value:
        if not isinstance(item, str) or not _FEATURE_RE.match(item):
            raise ValueError(f"feature tags must be snake_case, got {item!r}")
        if item not in tags:
            tags.append(item)
    return tuple(tags)


_PARSERS: dict[str, Callable[[Any], Any]] = {
    "name": _text(required=True),
    "description": _text(required=False),
    "daily_active_users": _number(int, lambda v: v > 0, "must be > 0"),
    "requests_per_user_per_day": _number(float, lambda v: v > 0, "must be > 0"),
    "read_write_ratio": _number(float, lambda v: v > 0, "must be > 0"),
    "avg_write_kb": _number(float, lambda v: v > 0, "must be > 0"),
    "latency_p99_ms": _number(int, lambda v: v > 0, "must be > 0"),
    "availability": _number(float, lambda v: 90 <= v < 100, "must be a percentage in [90, 100)"),
    "retention_years": _number(float, lambda v: v > 0, "must be > 0"),
    "peak_factor": _number(float, lambda v: v >= 1, "must be >= 1"),
    "regions": _number(int, lambda v: 1 <= v <= 10, "must be between 1 and 10"),
    "budget": _choice(BUDGET_LEVELS),
    "consistency": _choice(CONSISTENCY_LEVELS),
    "features": _features,
    "data_model": _choice(DATA_MODELS),
    "write_pattern": _choice(WRITE_PATTERNS),
    "traffic_pattern": _choice(TRAFFIC_PATTERNS),
    "ordering": _choice(ORDERING_LEVELS),
    "hot_keys": _flag,
    "multi_tenant": _flag,
    "compliance": _regimes,
    "team_size": _choice(TEAM_SIZES),
    "rpo_minutes": _number(int, lambda v: v >= 0, "must be >= 0"),
    "rto_minutes": _number(int, lambda v: v >= 0, "must be >= 0"),
}


def requirements_json_schema() -> dict[str, Any]:
    """JSON schema handed to the model so its output is shaped like Requirements."""
    number = {"type": "number"}
    integer = {"type": "integer"}
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "daily_active_users": integer,
            "requests_per_user_per_day": number,
            "read_write_ratio": number,
            "avg_write_kb": number,
            "latency_p99_ms": integer,
            "availability": number,
            "consistency": {"type": "string", "enum": list(CONSISTENCY_LEVELS)},
            "retention_years": number,
            "peak_factor": number,
            "regions": integer,
            "budget": {"type": "string", "enum": list(BUDGET_LEVELS)},
            "features": {"type": "array", "items": {"type": "string"}},
            "data_model": {"type": "string", "enum": list(DATA_MODELS)},
            "write_pattern": {"type": "string", "enum": list(WRITE_PATTERNS)},
            "traffic_pattern": {"type": "string", "enum": list(TRAFFIC_PATTERNS)},
            "ordering": {"type": "string", "enum": list(ORDERING_LEVELS)},
            "hot_keys": {"type": "boolean"},
            "multi_tenant": {"type": "boolean"},
            "compliance": {
                "type": "array",
                "items": {"type": "string", "enum": list(COMPLIANCE_REGIMES)},
            },
            "team_size": {"type": "string", "enum": list(TEAM_SIZES)},
            "rpo_minutes": integer,
            "rto_minutes": integer,
        },
        "required": [
            "name",
            "daily_active_users",
            "requests_per_user_per_day",
            "read_write_ratio",
            "avg_write_kb",
            "latency_p99_ms",
            "availability",
            "consistency",
            "features",
        ],
        "additionalProperties": False,
    }
