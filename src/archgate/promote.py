"""Turn model suggestions a human approved into draft rules, so the next run is deterministic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from archgate.design import SOURCE_LLM
from archgate.errors import ArchGateError, KnowledgeBaseError
from archgate.knowledge import KnowledgeBase, load_knowledge_base
from archgate.pipeline import Run, State

LEARNED_PRIORITY = 40


def draft_rules(run: Run, kb: KnowledgeBase) -> list[dict[str, Any]]:
    if run.state is not State.APPROVED:
        raise ArchGateError(f"run is {run.state.value}; only approved runs can be promoted")

    uncovered = _uncovered_features(run)
    if not uncovered:
        return []
    already = {f for rule in kb.rules for f in rule.covers}
    features = [f for f in uncovered if f not in already]
    if not features:
        return []

    # only gap-filling suggestions are tied to a feature; repair suggestions aren't
    suggested: dict[str, str] = {}
    for event in run.trace.events:
        if event.state == State.PROPOSE.value and event.action == "proposed_add":
            cid = event.detail["component"]
            choice = run.design.choices.get(cid)
            if choice and SOURCE_LLM in choice.sources:
                suggested[cid] = event.detail.get("why", "")
    if not suggested:
        return []

    by = run.decision["by"] if run.decision else "unknown"
    reasons = "; ".join(f"{cid}: {why}" for cid, why in suggested.items())
    conditions = [{"field": "features", "op": "contains", "value": f} for f in features]
    return [
        {
            "id": _next_id(kb),
            "name": f"Learned: {', '.join(features)}",
            "priority": LEARNED_PRIORITY,
            "covers": features,
            "when": conditions[0] if len(conditions) == 1 else {"all": conditions},
            "then": {"add": kb.catalog.order(set(suggested))},
            "why": f"{reasons} (model suggestion approved by {by} in run {run.run_id})",
            "learned_from": {"run": run.run_id, "approved_by": by,
                             "at": run.decision["at"] if run.decision else None},
        }
    ]


def append_rules(path: Path, drafts: list[dict[str, Any]], kb_loader=load_knowledge_base) -> None:
    """Validate the combined rule file before writing, so a bad draft never lands on disk."""
    path = Path(path)
    existing: dict[str, Any] = {"version": 1, "rules": []}
    if path.exists():
        existing = yaml.safe_load(path.read_text()) or existing
        existing.setdefault("rules", [])
    existing["rules"] = list(existing["rules"]) + drafts

    tmp = path.with_suffix(".tmp.yaml")
    tmp.write_text(_HEADER + yaml.safe_dump(existing, sort_keys=False))
    try:
        kb_loader(learned_path=tmp)
    except KnowledgeBaseError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


def to_yaml(drafts: list[dict[str, Any]]) -> str:
    return yaml.safe_dump({"rules": drafts}, sort_keys=False)


_HEADER = "# Rules promoted from approved runs. Review and edit like any other rule.\n"


def _uncovered_features(run: Run) -> list[str]:
    for event in run.trace.events:
        if event.actor == "rule" and event.action == "coverage":
            return list(event.detail.get("uncovered_features", []))
    return []


def _next_id(kb: KnowledgeBase) -> str:
    taken = {r.id for r in kb.rules}
    n = 1
    while f"L{n:03d}" in taken:
        n += 1
    return f"L{n:03d}"
