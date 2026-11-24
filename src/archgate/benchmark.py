"""Scores designs against reference cases so rule changes can be measured, not guessed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from archgate.errors import KnowledgeBaseError, RequirementsError
from archgate.knowledge import Catalog
from archgate.pipeline import Pipeline, Run
from archgate.requirements import Requirements

# Always present, so they say nothing about over-engineering.
BASELINE = frozenset({"client", "load_balancer", "app_service", "auth_service", "monitoring",
                      "backup_service"})


@dataclass(frozen=True)
class Expectation:
    """One required component, or a set of acceptable alternatives."""

    options: tuple[str, ...]

    def met_by(self, components: set[str]) -> bool:
        return any(opt in components for opt in self.options)

    def __str__(self) -> str:
        return " | ".join(self.options)


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    requirements: Requirements
    must: tuple[Expectation, ...]
    must_not: tuple[str, ...] = ()
    nice: tuple[str, ...] = ()
    settings: tuple[tuple[str, str], ...] = ()
    notes: str = ""


@dataclass
class CaseResult:
    case: Case
    components: set[str]
    missing: list[str]
    forbidden: list[str]
    extra: list[str]
    settings_hit: int
    settings_wrong: list[str]
    confidence: str | None

    @property
    def recall(self) -> float:
        total = len(self.case.must)
        return 1.0 if total == 0 else (total - len(self.missing)) / total

    @property
    def passed(self) -> bool:
        return not (self.missing or self.forbidden or self.settings_wrong)


@dataclass
class Report:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return _mean([1.0 if r.passed else 0.0 for r in self.results])

    @property
    def mean_recall(self) -> float:
        return _mean([r.recall for r in self.results])

    @property
    def violations(self) -> int:
        return sum(len(r.forbidden) for r in self.results)

    @property
    def settings_accuracy(self) -> float:
        total = sum(len(r.case.settings) for r in self.results)
        hits = sum(r.settings_hit for r in self.results)
        return 1.0 if total == 0 else hits / total

    @property
    def mean_extra(self) -> float:
        return _mean([len(r.extra) for r in self.results])

    def summary(self) -> dict[str, Any]:
        return {
            "cases": len(self.results),
            "passed": sum(r.passed for r in self.results),
            "pass_rate": round(self.pass_rate, 3),
            "component_recall": round(self.mean_recall, 3),
            "forbidden_components": self.violations,
            "settings_accuracy": round(self.settings_accuracy, 3),
            "avg_extra_components": round(self.mean_extra, 2),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "cases": [
                {
                    "id": r.case.id,
                    "passed": r.passed,
                    "recall": round(r.recall, 3),
                    "missing": r.missing,
                    "forbidden": r.forbidden,
                    "settings_wrong": r.settings_wrong,
                    "extra": r.extra,
                    "confidence": r.confidence,
                }
                for r in self.results
            ],
        }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def load_cases(folder: Path, catalog: Catalog) -> list[Case]:
    files = sorted(Path(folder).glob("*.yaml"))
    if not files:
        raise KnowledgeBaseError(f"no benchmark cases in {folder}")
    return [_load_case(path, catalog) for path in files]


def _load_case(path: Path, catalog: Catalog) -> Case:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise KnowledgeBaseError(f"{path.name}: invalid YAML: {exc}") from exc
    expect = data.get("expect") or {}
    try:
        req = Requirements.from_dict(data.get("requirements"))
    except RequirementsError as exc:
        raise KnowledgeBaseError(f"{path.name}: {exc}") from exc

    must = []
    for item in expect.get("must") or []:
        options = tuple(item["any_of"]) if isinstance(item, dict) else (item,)
        must.append(Expectation(options))
    must_not = tuple(expect.get("must_not") or ())
    nice = tuple(expect.get("nice") or ())
    settings = tuple((str(k), str(v)) for k, v in (expect.get("settings") or {}).items())

    referenced = [opt for e in must for opt in e.options] + list(must_not) + list(nice)
    unknown = [cid for cid in referenced if cid not in catalog]
    if unknown:
        raise KnowledgeBaseError(f"{path.name}: unknown components {unknown}")
    for key, value in settings:
        spec = catalog.settings.get(key)
        if spec is None or value not in spec.values:
            raise KnowledgeBaseError(f"{path.name}: invalid expected setting {key}={value}")

    return Case(
        id=path.stem,
        title=data.get("title", path.stem),
        requirements=req,
        must=tuple(must),
        must_not=must_not,
        nice=nice,
        settings=settings,
        notes=data.get("notes", ""),
    )


def score(case: Case, run: Run) -> CaseResult:
    components = run.design.components
    missing = [str(e) for e in case.must if not e.met_by(components)]
    forbidden = sorted(c for c in case.must_not if c in components)
    expected = {opt for e in case.must for opt in e.options} | set(case.nice)
    extra = sorted(components - expected - BASELINE)

    hits, wrong = 0, []
    for key, value in case.settings:
        actual = run.design.settings.get(key)
        if actual is not None and actual.value == value:
            hits += 1
        else:
            wrong.append(f"{key}: expected {value}, got {actual.value if actual else 'unset'}")

    return CaseResult(case, components, missing, forbidden, extra, hits, wrong, run.confidence)


def run_benchmark(cases: list[Case], pipeline: Pipeline) -> Report:
    report = Report()
    for case in cases:
        report.results.append(score(case, pipeline.run(case.requirements, run_id=case.id)))
    return report


def format_report(report: Report, verbose: bool = False) -> str:
    lines = [f"{'case':<28} {'pass':<5} {'recall':>6}  problems"]
    for r in report.results:
        problems = []
        if r.missing:
            problems.append("missing: " + ", ".join(r.missing))
        if r.forbidden:
            problems.append("forbidden: " + ", ".join(r.forbidden))
        if r.settings_wrong:
            problems.append("settings: " + "; ".join(r.settings_wrong))
        if verbose and r.extra:
            problems.append("extra: " + ", ".join(r.extra))
        mark = "ok" if r.passed else "FAIL"
        lines.append(f"{r.case.id:<28} {mark:<5} {r.recall:>6.0%}  {' / '.join(problems)}")

    s = report.summary()
    lines += [
        "",
        f"passed {s['passed']}/{s['cases']} ({s['pass_rate']:.0%})   "
        f"component recall {s['component_recall']:.0%}   "
        f"forbidden {s['forbidden_components']}   "
        f"settings {s['settings_accuracy']:.0%}   "
        f"avg extra {s['avg_extra_components']}",
    ]
    return "\n".join(lines)
