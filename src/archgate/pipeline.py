from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from archgate import __version__, assistant
from archgate.design import SOURCE_LLM, SOURCE_VALIDATOR, Design
from archgate.errors import (
    BudgetExceeded,
    IllegalTransition,
    LLMError,
    RequirementsError,
)
from archgate.estimator import Estimate, estimate
from archgate.knowledge import KnowledgeBase
from archgate.llm import LLMClient
from archgate.requirements import Requirements
from archgate.rules_engine import RuleEngine, build_facts, coverage_gaps, prune_settings
from archgate.trace import Trace
from archgate.validator import Finding, validate


class State(str, Enum):
    INTAKE = "intake"
    ESTIMATE = "estimate"
    RULES = "rules"
    PROPOSE = "propose"
    VALIDATE = "validate"
    REPAIR = "repair"
    EXPLAIN = "explain"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


TRANSITIONS: dict[State, frozenset[State]] = {
    State.INTAKE: frozenset({State.ESTIMATE, State.FAILED}),
    State.ESTIMATE: frozenset({State.RULES}),
    State.RULES: frozenset({State.PROPOSE, State.VALIDATE}),
    State.PROPOSE: frozenset({State.VALIDATE}),
    State.VALIDATE: frozenset({State.REPAIR, State.EXPLAIN}),
    # REPAIR -> EXPLAIN is the escalation path when no fix is possible
    State.REPAIR: frozenset({State.VALIDATE, State.EXPLAIN}),
    State.EXPLAIN: frozenset({State.AWAITING_APPROVAL}),
    State.AWAITING_APPROVAL: frozenset({State.APPROVED, State.REJECTED}),
    State.APPROVED: frozenset(),
    State.REJECTED: frozenset(),
    State.FAILED: frozenset(),
}

CONFIDENCE_HIGH = "high"
CONFIDENCE_REVIEW = "review_needed"
CONFIDENCE_BLOCKED = "blocked"


@dataclass
class Budget:
    max_llm_calls: int = 6
    max_repair_rounds: int = 3
    max_seconds: float = 600.0
    llm_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def charge_llm_call(self) -> None:
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded(f"LLM call budget of {self.max_llm_calls} used up")
        if time.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded(f"run exceeded {self.max_seconds:.0f}s")
        self.llm_calls += 1


@dataclass
class Run:
    run_id: str
    created_at: str
    state: State = State.INTAKE
    requirements: Requirements | None = None
    requirements_source: str = "file"
    estimate: Estimate | None = None
    design: Design = field(default_factory=Design)
    findings: list[Finding] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    explanation: str = ""
    confidence: str | None = None
    llm: str | None = None
    llm_calls: int = 0
    error: str | None = None
    decision: dict[str, Any] | None = None
    versions: dict[str, Any] = field(default_factory=dict)
    trace: Trace = field(default_factory=Trace)

    def transition(self, target: State, reason: str = "") -> None:
        if target not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {target.value} is not allowed")
        self.trace.record(self.state.value, "pipeline", "transition", to=target.value,
                          reason=reason)
        self.state = target

    def decide(self, approve: bool, by: str, note: str = "", force: bool = False) -> None:
        """The human gate. Nothing downstream may treat a design as final before this."""
        if self.state is not State.AWAITING_APPROVAL:
            raise IllegalTransition(f"run is {self.state.value}, not awaiting approval")
        if not by.strip():
            raise ValueError("a decision needs the reviewer's name")
        if approve and self.confidence == CONFIDENCE_BLOCKED and not force:
            raise ValueError("design has unresolved validation failures; use force to override")
        if not approve and not note.strip():
            raise ValueError("a rejection needs a note explaining why")
        target = State.APPROVED if approve else State.REJECTED
        self.decision = {
            "approved": approve,
            "by": by,
            "note": note,
            "forced": bool(force and approve and self.confidence == CONFIDENCE_BLOCKED),
            "at": _now(),
        }
        self.trace.record(self.state.value, "human", target.value, by=by, note=note,
                          forced=self.decision["forced"])
        self.transition(target, reason=f"decision by {by}")


ConfirmRequirements = Callable[[Requirements], Requirements | None]


class Pipeline:
    def __init__(
        self,
        kb: KnowledgeBase,
        llm: LLMClient | None = None,
        budget_factory: Callable[[], Budget] = Budget,
        confirm_requirements: ConfirmRequirements | None = None,
    ):
        self.kb = kb
        self.engine = RuleEngine(kb)
        self.llm = llm
        self.budget_factory = budget_factory
        self.confirm_requirements = confirm_requirements

    def run(self, source: Requirements | str, run_id: str | None = None) -> Run:
        run = Run(run_id=run_id or new_run_id(), created_at=_now(),
                  llm=self.llm.name if self.llm else None)
        # enough to reproduce or explain this run later
        run.versions = {
            "archgate": __version__,
            "knowledge": self.kb.version,
            "rules": len(self.kb.rules),
            "model": run.llm,
        }
        run.trace.record(run.state.value, "pipeline", "started", **run.versions)
        budget = self.budget_factory()
        try:
            if not self._intake(run, source, budget):
                return run
            run.transition(State.ESTIMATE)
            self._estimate(run)
            run.transition(State.RULES)
            gaps = self._apply_rules(run)
            if gaps["missing_slots"] or gaps["uncovered_features"]:
                run.transition(State.PROPOSE, reason="rules did not cover everything")
                self._propose(run, gaps, budget)
            run.transition(State.VALIDATE)
            self._validate_and_repair(run, budget)
            run.transition(State.EXPLAIN)
            self._explain(run, budget)
            run.confidence = self._confidence(run)
            run.trace.record(run.state.value, "pipeline", "confidence", level=run.confidence)
            run.transition(State.AWAITING_APPROVAL)
        finally:
            run.llm_calls = budget.llm_calls
        return run

    # --- stages -----------------------------------------------------------

    def _intake(self, run: Run, source: Requirements | str, budget: Budget) -> bool:
        if isinstance(source, Requirements):
            run.requirements = source
            run.trace.record(run.state.value, "code", "requirements_loaded", name=source.name)
            return True

        if self.llm is None:
            return self._fail(run, "free-text input needs a model; pass a requirements file")

        errors: list[str] = []
        for attempt in (1, 2):
            try:
                budget.charge_llm_call()
                req = assistant.extract_requirements(self.llm, source, errors or None)
            except RequirementsError as exc:
                errors = exc.problems
                run.trace.record(run.state.value, "llm", "extraction_rejected",
                                 attempt=attempt, problems=errors)
                continue
            except (LLMError, BudgetExceeded) as exc:
                return self._fail(run, str(exc))

            run.trace.record(run.state.value, "llm", "requirements_extracted",
                             attempt=attempt, requirements=req.to_dict())
            if self.confirm_requirements is not None:
                confirmed = self.confirm_requirements(req)
                if confirmed is None:
                    return self._fail(run, "requirements were not confirmed")
                if confirmed != req:
                    run.trace.record(run.state.value, "human", "requirements_edited",
                                     requirements=confirmed.to_dict())
                req = confirmed
            run.requirements = req
            run.requirements_source = "llm"
            return True

        return self._fail(run, "could not extract valid requirements: " + "; ".join(errors))

    def _estimate(self, run: Run) -> None:
        run.estimate = estimate(run.requirements)
        run.trace.record(run.state.value, "code", "estimated", **run.estimate.to_dict())

    def _apply_rules(self, run: Run) -> dict[str, list[str]]:
        outcome = self.engine.run(build_facts(run.requirements, run.estimate))
        run.design = outcome.design
        for rule in outcome.fired:
            run.trace.record(run.state.value, "rule", "fired", rule=rule.id, name=rule.name,
                             add=list(rule.add), remove=list(rule.remove))
        for res in outcome.resolutions:
            run.trace.record(run.state.value, "rule", "conflict_resolved", kind=res.kind,
                             component=res.component, outcome=res.outcome, detail=res.detail)
        gaps = coverage_gaps(self.kb.catalog, run.design, run.requirements,
                             outcome.covered_features)
        run.trace.record(run.state.value, "rule", "coverage", **gaps)
        return gaps

    def _propose(self, run: Run, gaps: dict[str, list[str]], budget: Budget) -> None:
        problems = [f"required slot '{s}' is empty" for s in gaps["missing_slots"]]
        problems += [
            f"feature '{f}' is not handled by any rule; add catalog components only if "
            "they clearly implement it, otherwise list it as unresolved"
            for f in gaps["uncovered_features"]
        ]
        if not self._ask_model(run, problems, budget, removable=None):
            run.unresolved += [f"feature '{f}' has no matching rule; needs human review"
                               for f in gaps["uncovered_features"]]

    def _validate_and_repair(self, run: Run, budget: Budget) -> None:
        seen = {frozenset(run.design.components)}
        rounds = 0
        while True:
            run.findings = validate(run.design, run.requirements, run.estimate,
                                    self.kb.catalog)
            run.trace.record(run.state.value, "validator", "checked",
                             findings=[f.to_dict() for f in run.findings])
            if not run.findings:
                return
            if rounds >= budget.max_repair_rounds:
                limit = budget.max_repair_rounds
                run.trace.record(run.state.value, "pipeline", "escalated",
                                 reason=f"repair limit of {limit} rounds reached")
                return

            run.transition(State.REPAIR)
            rounds += 1
            changed = self._auto_fix(run, run.findings)
            if not changed:
                fixable = [f for f in run.findings if f.design_fixable]
                removable = {c for f in fixable for c in f.involves}
                removable |= {cid for cid, ch in run.design.choices.items()
                              if not ch.deterministic}
                changed = bool(fixable) and self._ask_model(
                    run, [f"{f.check}: {f.message}" for f in fixable], budget, removable
                )

            for key in prune_settings(run.design, self.kb.catalog):
                run.trace.record(run.state.value, "pipeline", "setting_dropped", setting=key,
                                 reason="its component was removed")
            state = frozenset(run.design.components)
            if not changed or state in seen:
                run.trace.record(run.state.value, "pipeline", "escalated",
                                 reason="no further progress possible without a human")
                return
            seen.add(state)
            run.transition(State.VALIDATE)

    def _auto_fix(self, run: Run, findings: list[Finding]) -> bool:
        before = run.design.components
        fixes = [f for f in findings if f.design_fixable and f.has_auto_fix]
        # adds first, then removes, so a removal from one check is never undone by another
        for f in fixes:
            for cid in f.fix_add:
                if run.design.add(cid, SOURCE_VALIDATOR, f.check, f.message):
                    run.trace.record(run.state.value, "validator", "auto_added",
                                     component=cid, check=f.check)
        for f in fixes:
            for cid in f.fix_remove:
                if run.design.remove(cid):
                    run.trace.record(run.state.value, "validator", "auto_removed",
                                     component=cid, check=f.check, why=f.message)
        return run.design.components != before

    def _ask_model(
        self, run: Run, problems: list[str], budget: Budget, removable: set[str] | None
    ) -> bool:
        """Returns True if the model changed the design. removable=None forbids removals."""
        if self.llm is None:
            run.trace.record(run.state.value, "pipeline", "llm_skipped",
                             reason="no model configured", problems=problems)
            return False

        context = {
            "requirements": run.requirements.to_dict(),
            "estimate": run.estimate.to_dict(),
            "components": self.kb.catalog.order(run.design.components),
        }
        try:
            budget.charge_llm_call()
            proposal = assistant.propose_changes(self.llm, self.kb.catalog, context, problems)
        except (LLMError, BudgetExceeded) as exc:
            run.trace.record(run.state.value, "llm", "proposal_failed", error=str(exc))
            run.unresolved.append(f"model could not help: {exc}")
            return False

        for cid in sorted(set(proposal.add) & set(proposal.remove)):
            del proposal.add[cid], proposal.remove[cid]
            run.trace.record(run.state.value, "pipeline", "proposal_contradiction",
                             component=cid, why="model asked to both add and remove it; ignored")

        changed = False
        for cid, why in proposal.add.items():
            if run.design.add(cid, SOURCE_LLM, "proposal", why):
                changed = True
                run.trace.record(run.state.value, "llm", "proposed_add", component=cid, why=why)
        for cid, why in proposal.remove.items():
            # guardrail: the model can't drop components that no finding implicates
            if removable is None or cid not in removable:
                reason = (
                    "removals are not allowed while filling gaps"
                    if removable is None
                    else "no validation failure involves this component"
                )
                run.trace.record(run.state.value, "pipeline", "proposal_rejected",
                                 component=cid, why=reason)
                continue
            if run.design.remove(cid):
                changed = True
                run.trace.record(run.state.value, "llm", "proposed_remove", component=cid,
                                 why=why)
        for note in proposal.unresolved:
            run.unresolved.append(f"model: {note}")
        run.trace.record(run.state.value, "llm", "proposal_applied", changed=changed,
                         unresolved=proposal.unresolved)
        return changed

    def _explain(self, run: Run, budget: Budget) -> None:
        if self.llm is None:
            return
        summary = {
            "requirements": run.requirements.to_dict(),
            "estimate": run.estimate.to_dict(),
            "components": {
                cid: self.kb.catalog.components[cid].label
                for cid in self.kb.catalog.order(run.design.components)
                if cid in self.kb.catalog
            },
            "open_findings": [f.message for f in run.findings],
        }
        try:
            budget.charge_llm_call()
            run.explanation = assistant.explain(self.llm, summary)
            run.trace.record(run.state.value, "llm", "explained")
        except (LLMError, BudgetExceeded) as exc:
            # explanation is nice-to-have; the rule rationale is always in the doc
            run.trace.record(run.state.value, "llm", "explain_failed", error=str(exc))

    # --- helpers ----------------------------------------------------------

    def _confidence(self, run: Run) -> str:
        if run.findings:
            return CONFIDENCE_BLOCKED
        llm_touched = any(not c.deterministic for c in run.design.choices.values())
        if llm_touched or run.unresolved or run.requirements_source == "llm":
            return CONFIDENCE_REVIEW
        return CONFIDENCE_HIGH

    def _fail(self, run: Run, message: str) -> bool:
        run.error = message
        run.trace.record(run.state.value, "pipeline", "failed", error=message)
        run.transition(State.FAILED, reason=message)
        return False


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
