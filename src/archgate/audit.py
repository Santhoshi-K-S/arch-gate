"""Finds dead, always-on, duplicate and heavily contested rules."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from archgate.estimator import estimate
from archgate.knowledge import KnowledgeBase, Rule
from archgate.requirements import Requirements
from archgate.rules_engine import RuleEngine, build_facts


@dataclass
class AuditReport:
    runs: int
    never_fired: list[str] = field(default_factory=list)
    always_fired: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    contested: list[tuple[str, int]] = field(default_factory=list)
    fire_counts: dict[str, int] = field(default_factory=dict)

    def format(self) -> str:
        lines = [f"audited {self.runs} requirement sets"]
        lines.append(f"\nnever fired ({len(self.never_fired)}): "
                     + (", ".join(self.never_fired) or "none"))
        lines.append(f"always fired, excluding unconditional ({len(self.always_fired)}): "
                     + (", ".join(self.always_fired) or "none"))
        lines.append(f"duplicate rules ({len(self.duplicates)}): "
                     + (", ".join(f"{a}={b}" for a, b in self.duplicates) or "none"))
        lines.append("most contested (component or setting: conflicts): "
                     + (", ".join(f"{k}: {n}" for k, n in self.contested[:8]) or "none"))
        return "\n".join(lines)


def _unconditional(rule: Rule) -> bool:
    return rule.when == {"all": []}


def _signature(rule: Rule) -> str:
    return json.dumps([rule.when, sorted(rule.add), sorted(rule.remove), sorted(rule.settings)],
                      sort_keys=True)


def audit(kb: KnowledgeBase, requirements: list[Requirements]) -> AuditReport:
    engine = RuleEngine(kb)
    fired: Counter[str] = Counter()
    conflicts: Counter[str] = Counter()
    for req in requirements:
        outcome = engine.run(build_facts(req, estimate(req)))
        fired.update(rule.id for rule in outcome.fired)
        conflicts.update(r.component for r in outcome.resolutions if r.contested)

    report = AuditReport(runs=len(requirements), fire_counts=dict(fired))
    for rule in kb.rules:
        if fired[rule.id] == 0:
            report.never_fired.append(rule.id)
        elif fired[rule.id] == len(requirements) and not _unconditional(rule):
            report.always_fired.append(rule.id)

    seen: dict[str, str] = {}
    for rule in kb.rules:
        sig = _signature(rule)
        if sig in seen:
            report.duplicates.append((seen[sig], rule.id))
        else:
            seen[sig] = rule.id

    report.contested = conflicts.most_common()
    return report
