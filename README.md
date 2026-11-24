# ArchGate

Requirements in, reviewed system design out. Rule-based, traceable, and human-approved before anything gets built.

ArchGate is a hybrid design generator. Deterministic code does everything it can (capacity math,
component selection, validation, auto-fixes). A small open model, run locally through Ollama,
only fills the gaps the rules can't cover, and everything it suggests is checked by the same
validator and marked as model-suggested. Nothing is final until a person approves it.

## How a run works

```
INTAKE → ESTIMATE → RULES → (PROPOSE) → VALIDATE ⇄ REPAIR → EXPLAIN → AWAITING_APPROVAL → APPROVED / REJECTED
```

| Step | Done by | What happens |
|---|---|---|
| Intake | code, or model for free text | Requirements are parsed and validated against a strict schema |
| Estimate | code | Peak QPS, read/write split, storage, egress |
| Rules | rule engine | YAML rules pick components; priorities settle conflicts |
| Propose | model (optional) | Only runs if a required slot is empty or a feature has no rule |
| Validate | code | SPOF, consistency, capacity, latency, multi-region and catalog checks |
| Repair | code first, then model | Deterministic fixes first; the model only for what's left |
| Explain | model (optional) | Short trade-off notes; rule rationale is always included |
| Gate | human | Approve or reject with a note; blocked designs need `--force` |

Guardrails:

- **Closed catalog.** The model can only choose from the component ids in `data/components.yaml`.
- **No silent removals.** The model can't remove rule-backed components unless a validation failure involves them.
- **Explicit states.** Only the transitions listed in `pipeline.TRANSITIONS` can happen.
- **Budgets.** Each run has a cap on model calls, repair rounds and wall time. When it hits a cap it escalates to a human instead of retrying.
- **Full trace.** Every decision is logged to `trace.jsonl` with its actor (`code`, `rule`, `validator`, `llm`, `human`).
- **Confidence.** Each run is rated `high` (rules only, all checks pass), `review_needed` (the model was involved, or something is unresolved) or `blocked` (a check still fails).

## Quick start

```bash
uv sync
uv run archgate design examples/news_feed.yaml          # rules only if Ollama isn't running
uv run archgate show runs/<run-id>
uv run archgate approve runs/<run-id> --note "looks good"
uv run archgate reject  runs/<run-id> --note "needs multi-region writes"
```

To turn on the model layer (free, runs locally):

```bash
ollama pull qwen2.5:3b
uv run archgate design examples/online_store.yaml
uv run archgate design --text "A ride-sharing app for 2M daily riders with live driver tracking"
```

Use `--model`, `ARCHGATE_MODEL` or `ARCHGATE_OLLAMA_URL` to point at a different model or server, and `--no-llm` to force rules only.

Each run writes to `runs/<run-id>/`:

| File | Contents |
|---|---|
| `design.md` | Design doc: requirements, estimates, Mermaid diagram, who decided each component, trade-offs |
| `design.mmd` | The diagram on its own |
| `trace.jsonl` | Every decision, in order |
| `run.json` | Full run state, used by `approve` and `reject` |
| `requirements.yaml` | The requirements the run used; edit and rerun |

## Requirements format

```yaml
name: Chat App
daily_active_users: 5_000_000
requests_per_user_per_day: 60
read_write_ratio: 2          # reads per write
avg_write_kb: 1
latency_p99_ms: 200
availability: 99.9           # percent
consistency: eventual        # strong | eventual
features: [chat, notifications, media]

# optional, with defaults
retention_years: 5
peak_factor: 3
regions: 1
budget: medium               # low | medium | high
data_model: relational       # relational | document | key_value | graph | time_series
write_pattern: balanced      # balanced | append_only | update_heavy
traffic_pattern: steady      # steady | spiky | seasonal
ordering: none               # none | per_key | global
hot_keys: false
multi_tenant: false
compliance: []               # pci | hipaa | gdpr | soc2
team_size: medium            # small | medium | large
rpo_minutes: 60              # acceptable data loss
rto_minutes: 240             # acceptable downtime
```

Features with rules today: `chat`, `realtime`, `collaboration`, `feed`, `media`, `file_upload`,
`search`, `semantic_search`, `recommendations`, `payments`, `geo`, `notifications`, `analytics`,
`batch_jobs`, `public_api`, `short_links`. Any other tag goes to the model or is flagged for review.

## What a design contains

- **Components:** 53 building blocks, each with who added it and why.
- **Configuration decisions:** 21 settings such as `shard_key`, `cache_strategy`,
  `queue_delivery`, `backup_strategy`, `failover`, `tenancy_model`. A setting is dropped
  automatically if its component isn't in the design.
- **Considered and rejected:** components that were proposed and then removed, with the reason.
- **Versions:** archgate version and a hash of the exact rules and catalog used, so any run can
  be reproduced or explained later.

## The rule base

Rules live in `src/archgate/data/rules/`, one file per area: baseline, traffic, data, caching,
async, realtime, search/analytics, reliability, security, compliance, cost, domain.

```yaml
- id: CA001
  name: Cache for read-dominated traffic
  priority: 40
  when:
    all:
      - {field: read_write_ratio, op: ">=", value: 10}
      - {field: peak_read_qps, op: ">=", value: 500}
  then: {add: [cache], set: {cache_strategy: cache-aside}}
  why: Reads dominate, so caching cuts database load and latency.
```

Priority bands (higher wins when rules disagree):

| Band | Priority |
|---|---|
| Compliance | 90 |
| Correctness (money, inventory) | 80 |
| Reliability | 70 |
| Scale / capacity | 60 |
| Cost trims | 50 |
| Features and patterns | 40 |
| Optimizations | 30 |
| Defaults | 10-20 |

- **Fields** in a condition can be any requirement field or estimator output.
- **Operators:** `> >= < <= == != in contains`, combined with `all`, `any` and `not`.
- **Actions:** `add`, `remove` and `set` (settings).
- **Checking:** `uv run archgate check-kb` fails loudly on unknown fields, components or setting values.

## Measuring the rule base

```bash
uv run archgate bench              # score against 29 reference designs (rules only)
uv run archgate bench --model qwen2.5:7b
uv run archgate audit-rules        # dead, always-on, duplicate and contested rules
```

`benchmarks/cases/` holds one reference problem per file: its requirements, the components a
solid design must have (`must`, with `any_of` for alternatives), ones it must not have, optional
ones, and expected settings. A case passes when nothing required is missing, nothing forbidden is
present and every expected setting matches.

The reference designs were written alongside the rules, so a high score shows the rules are
consistent with them, not that they are right. Adding cases written by someone else is the best
way to find real gaps.

Tests also include:

- **Golden snapshots** (`tests/golden/`): CI fails if a rule change moves any benchmark design.
  Refresh after an intended change with `ARCHGATE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`.
- **Property tests:** 300 random requirement sets, checking invariants like "one primary
  database", "every dependency met", "three nines never has a single database" and determinism.

## Learning from approved runs

When the model fills a gap (a feature no rule covers) and a reviewer approves the design:

```bash
uv run archgate promote runs/<run-id>
```

This drafts a rule from the approved suggestion, shows it, and on confirmation adds it to
`learned_rules.yaml` (or `--learned-rules PATH`). From then on that feature is handled by a rule,
with no model call. Learned rules are versioned into the knowledge-base hash like any other.

## Development

```bash
uv run pytest
uv run ruff check .
```

## Roadmap

1. Rule engine, estimator, validator, approval gate (done)
2. Benchmark, golden and property tests, rule audit, versioning, rule promotion (done)
3. A tiny transformer trained from scratch on rule-generated designs
4. Swap it in as the model behind `LLMClient` and score it with `archgate bench`
