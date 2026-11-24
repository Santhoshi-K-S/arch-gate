from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from archgate.design import Design
from archgate.errors import ArchGateError
from archgate.estimator import Estimate
from archgate.knowledge import Catalog
from archgate.pipeline import Run, State
from archgate.render import markdown, mermaid
from archgate.requirements import Requirements
from archgate.trace import Trace
from archgate.validator import Finding

RUN_FILE = "run.json"


def save_run(run: Run, root: Path, catalog: Catalog) -> Path:
    run_dir = Path(root) / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / RUN_FILE, to_dict(run, catalog))
    with (run_dir / "trace.jsonl").open("w") as fh:
        for event in run.trace.events:
            fh.write(json.dumps(event.to_dict()) + "\n")

    if run.requirements is not None:
        (run_dir / "requirements.yaml").write_text(
            yaml.safe_dump(run.requirements.to_dict(), sort_keys=False)
        )
    if run.estimate is not None:
        (run_dir / "design.mmd").write_text(mermaid(run.design, catalog))
        (run_dir / "design.md").write_text(markdown(run, catalog))
    return run_dir


def load_run(run_dir: Path) -> Run:
    path = Path(run_dir) / RUN_FILE
    if not path.exists():
        raise ArchGateError(f"no run found at {run_dir}")
    return from_dict(json.loads(path.read_text()))


def from_dict(data: dict[str, Any]) -> Run:
    return Run(
        run_id=data["run_id"],
        created_at=data["created_at"],
        state=State(data["state"]),
        requirements=Requirements.from_dict(data["requirements"])
        if data.get("requirements")
        else None,
        requirements_source=data.get("requirements_source", "file"),
        estimate=Estimate(**data["estimate"]) if data.get("estimate") else None,
        design=Design.from_dicts(data.get("design") or {}, data.get("settings")),
        findings=[Finding(**_finding_args(f)) for f in data.get("findings", [])],
        unresolved=data.get("unresolved", []),
        explanation=data.get("explanation", ""),
        confidence=data.get("confidence"),
        llm=data.get("llm"),
        llm_calls=data.get("llm_calls", 0),
        error=data.get("error"),
        decision=data.get("decision"),
        versions=data.get("versions", {}),
        trace=Trace.from_dicts(data.get("trace", [])),
    )


def to_dict(run: Run, catalog: Catalog) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "created_at": run.created_at,
        "state": run.state.value,
        "confidence": run.confidence,
        "requirements": run.requirements.to_dict() if run.requirements else None,
        "requirements_source": run.requirements_source,
        "estimate": run.estimate.to_dict() if run.estimate else None,
        "design": run.design.components_dict(catalog.order(run.design.components)),
        "settings": run.design.settings_dict(),
        "findings": [f.to_dict() for f in run.findings],
        "unresolved": run.unresolved,
        "explanation": run.explanation,
        "llm": run.llm,
        "llm_calls": run.llm_calls,
        "error": run.error,
        "decision": run.decision,
        "versions": run.versions,
        "trace": [e.to_dict() for e in run.trace.events],
    }


def _finding_args(data: dict[str, Any]) -> dict[str, Any]:
    args = dict(data)
    for key in ("fix_add", "fix_remove", "involves"):
        args[key] = tuple(args.get(key, ()))
    return args


def _write_json(path: Path, data: dict[str, Any]) -> None:
    # write then rename so a crash never leaves a half-written run file
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
