"""Turn the HTML form (or pasted YAML) into the dict Requirements.from_dict expects."""

from __future__ import annotations

from typing import Any

import yaml

from archgate.errors import RequirementsError

NUMBER_FIELDS = (
    "daily_active_users", "requests_per_user_per_day", "read_write_ratio", "avg_write_kb",
    "latency_p99_ms", "availability", "retention_years", "peak_factor", "regions",
    "rpo_minutes", "rto_minutes",
)
CHOICE_FIELDS = (
    "consistency", "budget", "data_model", "write_pattern", "traffic_pattern", "ordering",
    "team_size",
)
FLAG_FIELDS = ("hot_keys", "multi_tenant")


def from_form(form: dict[str, Any], compliance: list[str]) -> dict[str, Any]:
    """Blank optional fields are left out so the model defaults apply."""
    data: dict[str, Any] = {}
    problems: list[str] = []

    for key in ("name", "description"):
        value = str(form.get(key, "")).strip()
        if value:
            data[key] = value

    for key in NUMBER_FIELDS:
        raw = str(form.get(key, "")).strip().replace(",", "").replace("_", "")
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError:
            problems.append(f"{key}: '{raw}' is not a number")
            continue
        data[key] = int(number) if number.is_integer() else number

    for key in CHOICE_FIELDS:
        value = str(form.get(key, "")).strip()
        if value:
            data[key] = value

    for key in FLAG_FIELDS:
        data[key] = form.get(key) == "on"

    features = str(form.get("features", ""))
    data["features"] = [f.strip().lower().replace(" ", "_")
                        for f in features.replace("\n", ",").split(",") if f.strip()]
    if compliance:
        data["compliance"] = compliance

    if problems:
        raise RequirementsError(problems)
    return data


def from_yaml(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RequirementsError([f"invalid YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise RequirementsError(["YAML must be a mapping of requirement fields"])
    return data
