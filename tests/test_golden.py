"""Snapshot of every benchmark design. Refresh after an intended change with:

    ARCHGATE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
"""

import json
import os
from pathlib import Path

import pytest

from archgate.benchmark import load_cases
from archgate.knowledge import load_knowledge_base
from archgate.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
KB = load_knowledge_base()
CASES = load_cases(ROOT / "benchmarks" / "cases", KB.catalog)


def snapshot(case) -> dict:
    run = Pipeline(KB).run(case.requirements, run_id=case.id)
    return {
        "components": KB.catalog.order(run.design.components),
        "settings": {k: v.value for k, v in sorted(run.design.settings.items())},
        "confidence": run.confidence,
        "findings": [f.check for f in run.findings],
    }


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_design_matches_golden(case):
    path = GOLDEN / f"{case.id}.json"
    actual = snapshot(case)
    if os.environ.get("ARCHGATE_UPDATE_GOLDEN"):
        path.write_text(json.dumps(actual, indent=2) + "\n")
    assert path.exists(), f"no golden file; run with ARCHGATE_UPDATE_GOLDEN=1 to create {path}"
    assert actual == json.loads(path.read_text())
