from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from archgate.design import SOURCE_RULE, Design
from archgate.estimator import Estimate
from archgate.knowledge import Catalog, KnowledgeBase, Rule
from archgate.requirements import Requirements

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    "in": lambda fact, value: fact in value,
    "contains": lambda fact, value: value in fact,
}


def build_facts(req: Requirements, est: Estimate) -> dict[str, Any]:
    return {**req.to_dict(), **est.to_dict()}


def evaluate(cond: dict[str, Any], facts: dict[str, Any]) -> bool:
    if "all" in cond:
        return all(evaluate(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, facts) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], facts)
    return _OPS[cond["op"]](facts[cond["field"]], cond["value"])


@dataclass(frozen=True)
class Vote:
    rule: Rule
    action: str  # "add" or "remove"


DEFAULT_PRIORITY_MAX = 20


@dataclass(frozen=True)
class Resolution:
    """A conflict the engine settled, kept for the trace."""

    kind: str
    component: str
    outcome: str
    detail: str
    # False when the loser was only a default; overriding defaults is expected
    contested: bool = True


@dataclass
class RuleOutcome:
    design: Design
    fired: list[Rule]
    resolutions: list[Resolution] = field(default_factory=list)
    covered_features: set[str] = field(default_factory=set)


class RuleEngine:
    def __init__(self, kb: KnowledgeBase):
        self.catalog: Catalog = kb.catalog
        self.rules = sorted(kb.rules, key=lambda r: (-r.priority, r.id))

    def run(self, facts: dict[str, Any]) -> RuleOutcome:
        fired = [rule for rule in self.rules if evaluate(rule.when, facts)]

        votes: dict[str, list[Vote]] = {}
        for rule in fired:
            for cid in rule.add:
                votes.setdefault(cid, []).append(Vote(rule, "add"))
            for cid in rule.remove:
                votes.setdefault(cid, []).append(Vote(rule, "remove"))

        outcome = RuleOutcome(design=Design(), fired=fired)
        for rule in fired:
            outcome.covered_features.update(rule.covers)

        add_priority: dict[str, int] = {}
        for cid, cast in votes.items():
            if self._resolve_votes(cid, cast, outcome):
                adds = [v.rule for v in cast if v.action == "add"]
                add_priority[cid] = max(r.priority for r in adds)
                for rule in adds:
                    outcome.design.add(cid, SOURCE_RULE, rule.id, rule.why)

        self._resolve_groups(outcome, add_priority)
        self._drop_unmet_requirements(outcome)
        self._resolve_settings(fired, outcome)
        return outcome

    def _resolve_settings(self, fired: list[Rule], outcome: RuleOutcome) -> None:
        # fired is already sorted by priority, so the first rule to set a key wins
        for rule in fired:
            for key, value in rule.settings:
                current = outcome.design.settings.get(key)
                if current is None:
                    outcome.design.set(key, value, SOURCE_RULE, rule.id, rule.why)
                elif current.value != value:
                    outcome.resolutions.append(
                        Resolution(
                            kind="setting",
                            component=key,
                            outcome=f"kept {current.value}",
                            detail=f"{rule.id} wanted {value}; {current.reason.ref} "
                            "has higher priority",
                            contested=rule.priority > DEFAULT_PRIORITY_MAX,
                        )
                    )
        for key in prune_settings(outcome.design, self.catalog):
            outcome.resolutions.append(
                Resolution(kind="setting", component=key, outcome="dropped",
                           detail="none of the components it applies to is in the design",
                           contested=False)
            )

    def _resolve_votes(self, cid: str, cast: list[Vote], outcome: RuleOutcome) -> bool:
        """Highest priority wins; on a tie, adding wins so we never silently lose a component."""
        top = max(v.rule.priority for v in cast)
        keep = any(v.action == "add" for v in cast if v.rule.priority == top)
        has_add = any(v.action == "add" for v in cast)
        has_remove = any(v.action == "remove" for v in cast)
        if has_add and has_remove:
            winners = sorted({v.rule.id for v in cast if v.rule.priority == top})
            outcome.resolutions.append(
                Resolution(
                    kind="vote",
                    component=cid,
                    outcome="kept" if keep else "removed",
                    detail=f"rules disagreed; priority {top} from {', '.join(winners)} decided",
                )
            )
        return keep

    def _resolve_groups(self, outcome: RuleOutcome, add_priority: dict[str, int]) -> None:
        for group, members in self.catalog.groups.items():
            present = [cid for cid in members if cid in outcome.design]
            if len(present) < 2:
                continue
            # ties go to the later (more scalable) member listed in the catalog
            winner = max(present, key=lambda cid: (add_priority.get(cid, 0), members.index(cid)))
            for cid in present:
                if cid != winner:
                    outcome.design.remove(cid)
                    outcome.resolutions.append(
                        Resolution(
                            kind="exclusive_group",
                            component=cid,
                            outcome="removed",
                            detail=f"only one {group} allowed; {winner} has higher priority",
                            contested=add_priority.get(cid, 0) > DEFAULT_PRIORITY_MAX,
                        )
                    )

    def _drop_unmet_requirements(self, outcome: RuleOutcome) -> None:
        changed = True
        while changed:
            changed = False
            for cid in list(outcome.design.components):
                needs = self.catalog.components[cid].requires_any
                if needs and not any(n in outcome.design for n in needs):
                    outcome.design.remove(cid)
                    outcome.resolutions.append(
                        Resolution(
                            kind="requires",
                            component=cid,
                            outcome="removed",
                            detail=f"needs one of {', '.join(needs)}",
                            contested=False,
                        )
                    )
                    changed = True


def prune_settings(design: Design, catalog: Catalog) -> list[str]:
    """Drop settings whose components are gone. Returns the dropped keys."""
    dropped = []
    for key in list(design.settings):
        spec = catalog.settings.get(key)
        if spec is None or not any(cid in design for cid in spec.applies_to):
            del design.settings[key]
            dropped.append(key)
    return dropped


def coverage_gaps(
    catalog: Catalog, design: Design, req: Requirements, covered: set[str]
) -> dict[str, list[str]]:
    """What the rules could not decide: empty slots and feature tags no rule understands."""
    missing_slots = [
        slot
        for slot, options in catalog.required_slots.items()
        if not any(cid in design for cid in options)
    ]
    uncovered = [f for f in req.features if f not in covered]
    return {"missing_slots": missing_slots, "uncovered_features": uncovered}
