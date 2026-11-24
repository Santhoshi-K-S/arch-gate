"""The model-facing tasks. Every output is schema-constrained and checked before use."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from archgate.errors import LLMError
from archgate.knowledge import Catalog
from archgate.llm import LLMClient
from archgate.requirements import Requirements, requirements_json_schema

EXTRACT_SYSTEM = """You turn a plain-English product description into system requirements.
Return only JSON matching the schema. Use these units:
- availability is a percentage, e.g. 99.9
- latency_p99_ms is milliseconds
- avg_write_kb is the average size of one stored write in kilobytes
- read_write_ratio is reads per write, e.g. 100 for a read-heavy app
- consistency is "strong" when money or inventory is involved, otherwise usually "eventual"
- features are short snake_case tags such as chat, feed, media, search, payments, geo,
  notifications, analytics, file_upload, batch_jobs, realtime, public_api, recommendations,
  semantic_search, short_links, collaboration
When a number is not stated, pick a reasonable value for the kind of product described.
Only include these optional fields when the description clearly implies them:
data_model, write_pattern, traffic_pattern, ordering, hot_keys, multi_tenant,
compliance (pci, hipaa, gdpr, soc2), team_size, rpo_minutes, rto_minutes."""

DESIGN_SYSTEM = """You are a senior system architect reviewing a draft architecture.
You may only use component ids from the provided catalog. Keep changes minimal:
fix the listed gaps or validation failures and nothing else.
Only add a component if it directly addresses a listed problem. If nothing in the catalog
clearly fits, add nothing and describe the problem in "unresolved". An empty answer is
better than a wrong component. Never put the same component in both "add" and "remove"."""

EXPLAIN_SYSTEM = """You explain an approved-for-review architecture to an engineering team.
Write 4-8 short bullet points about the main trade-offs and risks. Plain language, no headings,
no restating of the component list, and do not invent numbers that are not given."""


@dataclass
class Proposal:
    add: dict[str, str] = field(default_factory=dict)
    remove: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.remove)


def extract_requirements(llm: LLMClient, text: str, previous_errors: list[str] | None = None):
    prompt = f"Product description:\n{text}"
    if previous_errors:
        prompt += "\n\nYour previous answer was rejected for these reasons, fix them:\n- "
        prompt += "\n- ".join(previous_errors)
    data = llm.complete_json(EXTRACT_SYSTEM, prompt, requirements_json_schema())
    # raises RequirementsError, which the pipeline feeds back on retry
    return Requirements.from_dict(data)


def _proposal_schema(catalog: Catalog) -> dict[str, Any]:
    change = {
        "type": "object",
        "properties": {
            "component": {"type": "string", "enum": list(catalog.components)},
            "reason": {"type": "string"},
        },
        "required": ["component", "reason"],
    }
    return {
        "type": "object",
        "properties": {
            "add": {"type": "array", "items": change},
            "remove": {"type": "array", "items": change},
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["add", "remove", "unresolved"],
    }


def _catalog_brief(catalog: Catalog) -> str:
    lines = []
    for comp in catalog.components.values():
        note = f" - {comp.tradeoff}" if comp.tradeoff else ""
        lines.append(f"{comp.id}: {comp.label}{note}")
    return "\n".join(lines)


def propose_changes(
    llm: LLMClient,
    catalog: Catalog,
    context: dict[str, Any],
    problems: list[str],
) -> Proposal:
    prompt = (
        f"Catalog:\n{_catalog_brief(catalog)}\n\n"
        f"Current state:\n{json.dumps(context, indent=2)}\n\n"
        "Problems to address:\n- " + "\n- ".join(problems)
    )
    data = llm.complete_json(DESIGN_SYSTEM, prompt, _proposal_schema(catalog))
    return _parse_proposal(data, catalog)


def _parse_proposal(data: dict[str, Any], catalog: Catalog) -> Proposal:
    proposal = Proposal()
    try:
        for key, target in (("add", proposal.add), ("remove", proposal.remove)):
            for item in data.get(key) or []:
                cid = item["component"]
                # the schema should already prevent this; never trust it alone
                if cid not in catalog:
                    raise LLMError(f"model proposed unknown component {cid!r}")
                target[cid] = str(item.get("reason", "")).strip() or "no reason given"
        proposal.unresolved = [str(u) for u in data.get("unresolved") or [] if str(u).strip()]
    except (KeyError, TypeError, AttributeError) as exc:
        raise LLMError(f"malformed proposal: {exc}") from exc
    return proposal


def explain(llm: LLMClient, summary: dict[str, Any]) -> str:
    prompt = f"Architecture under review:\n{json.dumps(summary, indent=2)}"
    return llm.complete_text(EXPLAIN_SYSTEM, prompt)
