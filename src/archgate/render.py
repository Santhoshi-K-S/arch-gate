from __future__ import annotations

from archgate.design import Design
from archgate.knowledge import Catalog

_SHAPES = {
    "datastore": ('[("', '")]'),
    "storage": ('[("', '")]'),
    "messaging": ('[["', '"]]'),
    "client": ('(["', '"])'),
}


def mermaid(design: Design, catalog: Catalog) -> str:
    present = [cid for cid in catalog.order(design.components) if cid in catalog]
    lines = ["flowchart LR"]
    for cid in present:
        comp = catalog.components[cid]
        left, right = _SHAPES.get(comp.kind, ('["', '"]'))
        lines.append(f"    {cid}{left}{comp.label}{right}")

    seen: set[tuple[str, str]] = set()
    for edge in catalog.edges:
        for src in edge.sources:
            if src not in design:
                continue
            targets = [t for t in edge.targets if t in design and t != src]
            if edge.mode == "first":
                targets = targets[:1]
            for dst in targets:
                if (src, dst) in seen:
                    continue
                seen.add((src, dst))
                arrow = "-.->" if edge.style == "dotted" else "-->"
                lines.append(f"    {src} {arrow} {dst}")

    llm_ids = [cid for cid in present if not design.choices[cid].deterministic]
    if llm_ids:
        lines.append("    classDef suggested stroke-dasharray: 5 5,stroke:#d97706")
        lines.append(f"    class {','.join(llm_ids)} suggested")
    return "\n".join(lines) + "\n"


def markdown(run, catalog: Catalog) -> str:
    req, est, design = run.requirements, run.estimate, run.design
    out = [
        f"# {req.name}",
        "",
        f"- Run: `{run.run_id}`",
        f"- Status: **{run.state.value}**",
        f"- Confidence: **{run.confidence}**",
        f"- Requirements source: {run.requirements_source}",
        f"- Model: {run.llm or 'none (rules only)'} ({run.llm_calls} calls)",
    ]
    if run.versions:
        out.append(f"- Knowledge base: `{run.versions.get('knowledge')}` "
                   f"({run.versions.get('rules')} rules), archgate {run.versions.get('archgate')}")
    if run.decision:
        verdict = "Approved" if run.decision["approved"] else "Rejected"
        forced = " (forced)" if run.decision["forced"] else ""
        out.append(f"- {verdict}{forced} by {run.decision['by']} at {run.decision['at']}")
        if run.decision["note"]:
            out.append(f"  - Note: {run.decision['note']}")
    if req.description:
        out += ["", req.description]

    out += ["", "## Requirements", "", "| Field | Value |", "|---|---|"]
    for key, value in req.to_dict().items():
        if key in ("name", "description"):
            continue
        shown = ", ".join(value) if isinstance(value, list) else value
        out.append(f"| {key} | {shown} |")

    out += ["", "## Capacity estimate", "", "| Metric | Value |", "|---|---|"]
    out += [
        f"| Average QPS | {est.avg_qps:,.0f} |",
        f"| Peak QPS | {est.peak_qps:,.0f} |",
        f"| Peak reads/s | {est.peak_read_qps:,.0f} |",
        f"| Peak writes/s | {est.peak_write_qps:,.0f} |",
        f"| Storage (raw) | {est.storage_tb:,.2f} TB |",
        f"| Storage (x3 replicated) | {est.replicated_storage_tb:,.2f} TB |",
        f"| Peak egress | {est.peak_egress_mbps:,.0f} Mbps |",
    ]

    out += ["", "## Architecture", "", "```mermaid", mermaid(design, catalog).rstrip(), "```"]
    out += ["", "Dashed outline = suggested by the model, not by a rule."]

    out += ["", "## Components", "", "| Component | Decided by | Why |", "|---|---|---|"]
    for cid in catalog.order(design.components):
        choice = design.choices[cid]
        label = catalog.components[cid].label if cid in catalog else cid
        refs = ", ".join(sorted({f"{r.source}:{r.ref}" for r in choice.reasons}))
        why = " / ".join(dict.fromkeys(r.why for r in choice.reasons))
        out.append(f"| {label} | {refs} | {why} |")

    if design.settings:
        out += ["", "## Configuration decisions", "", "| Setting | Value | Decided by | Why |",
                "|---|---|---|---|"]
        for key in sorted(design.settings):
            setting = design.settings[key]
            r = setting.reason
            out.append(f"| {key} | {setting.value} | {r.source}:{r.ref} | {r.why} |")

    rejected, blocked = rejected_alternatives(run)
    if rejected:
        out += ["", "## Considered and rejected", "", "| Component | Rejected by | Reason |",
                "|---|---|---|"]
        out += [f"| {cid} | {by} | {why} |" for cid, by, why in rejected]
    if blocked:
        out += ["", "## Blocked model changes", ""]
        out += [f"- {cid}: {why}" for cid, why in blocked]

    tradeoffs = [
        f"- **{catalog.components[cid].label}**: {catalog.components[cid].tradeoff}"
        for cid in catalog.order(design.components)
        if cid in catalog and catalog.components[cid].tradeoff
    ]
    if tradeoffs:
        out += ["", "## Trade-offs", "", *tradeoffs]

    if run.explanation:
        out += ["", "## Reviewer notes (model-written)", "", run.explanation]

    if run.findings:
        out += ["", "## Open validation failures", ""]
        out += [f"- `{f.check}` {f.message}" for f in run.findings]
    if run.unresolved:
        out += ["", "## Needs human review", ""]
        out += [f"- {item}" for item in run.unresolved]

    return "\n".join(out) + "\n"



def rejected_alternatives(run) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Components that were on the table but are not in the final design, from the trace."""
    rejected: dict[str, tuple[str, str, str]] = {}
    blocked: list[tuple[str, str]] = []
    for event in run.trace.events:
        d = event.detail
        if event.action == "conflict_resolved" and d.get("outcome") == "removed":
            rejected[d["component"]] = (d["component"], f"rule engine ({d['kind']})", d["detail"])
        elif event.action == "auto_removed":
            rejected[d["component"]] = (d["component"], f"validator:{d['check']}",
                                        d.get("why", "failed a validation check"))
        elif event.action == "proposed_remove":
            rejected[d["component"]] = (d["component"], "llm", d.get("why", ""))
        elif event.action in ("proposal_rejected", "proposal_contradiction"):
            blocked.append((d["component"], d["why"]))
    final = run.design.components
    return [r for cid, r in rejected.items() if cid not in final], blocked
