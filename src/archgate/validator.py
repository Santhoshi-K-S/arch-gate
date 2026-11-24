from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from archgate.design import Design
from archgate.estimator import Estimate
from archgate.knowledge import Catalog
from archgate.requirements import Requirements

POSTGRES_MAX_STORAGE_TB = 10
POSTGRES_MAX_WRITE_QPS = 5000


@dataclass(frozen=True)
class Finding:
    check: str
    message: str
    fix_add: tuple[str, ...] = ()
    fix_remove: tuple[str, ...] = ()
    involves: tuple[str, ...] = ()
    # False when no design change can fix it (the requirements themselves conflict)
    design_fixable: bool = True

    @property
    def has_auto_fix(self) -> bool:
        return bool(self.fix_add or self.fix_remove)

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "message": self.message,
            "fix_add": list(self.fix_add),
            "fix_remove": list(self.fix_remove),
            "involves": list(self.involves),
            "design_fixable": self.design_fixable,
        }


@dataclass
class Context:
    design: Design
    req: Requirements
    est: Estimate
    catalog: Catalog
    findings: list[Finding] = field(default_factory=list)

    def has(self, cid: str) -> bool:
        return cid in self.design

    def primary_dbs(self) -> list[str]:
        return [c for c in self.catalog.required_slots.get("primary_db", ()) if self.has(c)]

    def flag(self, check: str, message: str, **kwargs) -> None:
        self.findings.append(Finding(check, message, **kwargs))


def _known_components(ctx: Context) -> None:
    for cid in sorted(ctx.design.components):
        if cid not in ctx.catalog:
            ctx.flag("V001", f"unknown component {cid!r}", fix_remove=(cid,), involves=(cid,))


def _required_slots(ctx: Context) -> None:
    for slot, options in ctx.catalog.required_slots.items():
        if not any(ctx.has(c) for c in options):
            # entry and compute have one obvious answer; the database choice does not
            fix = (options[0],) if len(options) == 1 else ()
            ctx.flag("V002", f"no component fills the {slot} slot", fix_add=fix)


def _exclusive_groups(ctx: Context) -> None:
    for group, members in ctx.catalog.groups.items():
        present = [c for c in members if ctx.has(c)]
        if len(present) > 1:
            ctx.flag(
                "V003", f"more than one {group}: {', '.join(present)}", involves=tuple(present)
            )


def _requirements_met(ctx: Context) -> None:
    for cid in sorted(ctx.design.components):
        comp = ctx.catalog.components.get(cid)
        if comp and comp.requires_any and not any(ctx.has(n) for n in comp.requires_any):
            ctx.flag(
                "V004",
                f"{cid} needs one of {', '.join(comp.requires_any)}",
                fix_remove=(cid,),
                involves=(cid,),
            )


def _single_point_of_failure(ctx: Context) -> None:
    if ctx.req.availability >= 99.9 and ctx.has("postgres") and not ctx.has("db_replicas"):
        ctx.flag(
            "V005",
            f"single PostgreSQL instance cannot meet {ctx.req.availability}% availability",
            fix_add=("db_replicas",),
            involves=("postgres",),
        )


def _consistency(ctx: Context) -> None:
    if ctx.req.consistency != "strong":
        return
    for cid in ctx.primary_dbs():
        if ctx.catalog.components[cid].consistency == "eventual":
            ctx.flag(
                "V006",
                f"{cid} is eventually consistent but strong consistency is required",
                fix_add=("sharded_sql",),
                fix_remove=(cid,),
                involves=(cid,),
            )


def _payments_consistency(ctx: Context) -> None:
    if "payments" in ctx.req.features and ctx.req.consistency != "strong":
        ctx.flag(
            "V007",
            "payments require strong consistency; update the requirements",
            design_fixable=False,
        )


def _postgres_capacity(ctx: Context) -> None:
    if not ctx.has("postgres"):
        return
    too_big = ctx.est.storage_tb > POSTGRES_MAX_STORAGE_TB
    too_hot = ctx.est.peak_write_qps > POSTGRES_MAX_WRITE_QPS
    if too_big or too_hot:
        target = "sharded_sql" if ctx.req.consistency == "strong" else "wide_column_store"
        ctx.flag(
            "V008",
            f"single-primary PostgreSQL will not hold {ctx.est.storage_tb:.1f} TB "
            f"/ {ctx.est.peak_write_qps:.0f} peak writes/s",
            fix_add=(target,),
            fix_remove=("postgres", "db_replicas"),
            involves=("postgres", "db_replicas"),
        )


def _latency(ctx: Context) -> None:
    if (
        ctx.req.latency_p99_ms <= 100
        and ctx.est.peak_read_qps >= 1000
        and not ctx.has("cache")
        and ctx.primary_dbs()
    ):
        ctx.flag(
            "V009",
            f"{ctx.est.peak_read_qps:.0f} reads/s at p99 <= "
            f"{ctx.req.latency_p99_ms}ms needs a cache",
            fix_add=("cache",),
        )


def _blob_storage(ctx: Context) -> None:
    wants_blobs = {"media", "file_upload"} & set(ctx.req.features)
    if wants_blobs and not ctx.has("object_storage"):
        ctx.flag("V010", "media/files need object storage", fix_add=("object_storage",))


def _multi_region(ctx: Context) -> None:
    if ctx.req.regions > 1 or ctx.req.availability >= 99.99:
        missing = tuple(c for c in ("dns_geo", "multi_region_replication") if not ctx.has(c))
        if missing:
            ctx.flag("V011", "multi-region requirements need geo routing and replication",
                     fix_add=missing)


def _dead_letters(ctx: Context) -> None:
    if ctx.has("message_queue") and not ctx.has("dead_letter_queue"):
        ctx.flag("V012", "a queue without a dead-letter queue retries poison messages forever",
                 fix_add=("dead_letter_queue",), involves=("message_queue",))


_COMPLIANCE_CONTROLS = {
    "pci": ("tokenization_vault", "audit_log", "kms"),
    "hipaa": ("tokenization_vault", "audit_log", "kms"),
    "gdpr": ("audit_log", "kms"),
    "soc2": ("audit_log", "kms"),
}


def _compliance_controls(ctx: Context) -> None:
    for regime in ctx.req.compliance:
        missing = tuple(c for c in _COMPLIANCE_CONTROLS.get(regime, ()) if not ctx.has(c))
        if missing:
            ctx.flag("V013", f"{regime.upper()} requires {', '.join(missing)}", fix_add=missing)


def _ledger_consistency(ctx: Context) -> None:
    if ctx.has("ledger") and ctx.req.consistency != "strong":
        ctx.flag("V014", "a ledger needs strong consistency; update the requirements",
                 design_fixable=False)


def _phi_at_the_edge(ctx: Context) -> None:
    if "hipaa" in ctx.req.compliance and ctx.has("cdn"):
        ctx.flag("V015", "PHI must not be cached on a third-party CDN",
                 fix_remove=("cdn",), involves=("cdn",))


def _hot_keys(ctx: Context) -> None:
    if ctx.req.hot_keys and not ctx.has("cache") and ctx.primary_dbs():
        ctx.flag("V016", "hot keys will overload one database partition without a cache",
                 fix_add=("cache",))


CHECKS: tuple[Callable[[Context], None], ...] = (
    _known_components,
    _required_slots,
    _exclusive_groups,
    _requirements_met,
    _single_point_of_failure,
    _consistency,
    _payments_consistency,
    _postgres_capacity,
    _latency,
    _blob_storage,
    _multi_region,
    _dead_letters,
    _compliance_controls,
    _ledger_consistency,
    _phi_at_the_edge,
    _hot_keys,
)


def validate(design: Design, req: Requirements, est: Estimate, catalog: Catalog) -> list[Finding]:
    ctx = Context(design, req, est, catalog)
    for check in CHECKS:
        check(ctx)
    return ctx.findings
