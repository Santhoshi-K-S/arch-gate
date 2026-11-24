from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from archgate.errors import KnowledgeBaseError
from archgate.estimator import Estimate
from archgate.requirements import Requirements

OPERATORS = (">", ">=", "<", "<=", "==", "!=", "in", "contains")
FACT_FIELDS = frozenset(f.name for f in fields(Requirements)) | frozenset(
    f.name for f in fields(Estimate)
)
ORIGIN_BUNDLED = "bundled"
ORIGIN_LEARNED = "learned"


@dataclass(frozen=True)
class Component:
    id: str
    label: str
    kind: str
    group: str | None = None
    consistency: str | None = None
    replicated: bool = False
    requires_any: tuple[str, ...] = ()
    tradeoff: str = ""


@dataclass(frozen=True)
class SettingSpec:
    """A configuration decision that only means something when one of its components exists."""

    key: str
    applies_to: tuple[str, ...]
    values: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class Edge:
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    mode: str = "all"
    style: str = "solid"


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    priority: int
    when: dict[str, Any]
    add: tuple[str, ...]
    remove: tuple[str, ...]
    why: str
    covers: tuple[str, ...] = ()
    settings: tuple[tuple[str, str], ...] = ()
    origin: str = ORIGIN_BUNDLED
    source: str = ""


@dataclass(frozen=True)
class Catalog:
    components: dict[str, Component]
    required_slots: dict[str, tuple[str, ...]]
    edges: tuple[Edge, ...]
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    settings: dict[str, SettingSpec] = field(default_factory=dict)

    def __contains__(self, component_id: str) -> bool:
        return component_id in self.components

    def order(self, ids: list[str] | set[str]) -> list[str]:
        """Sort ids in catalog order so output is stable."""
        position = {cid: i for i, cid in enumerate(self.components)}
        return sorted(ids, key=lambda cid: position.get(cid, len(position)))


@dataclass(frozen=True)
class KnowledgeBase:
    catalog: Catalog
    rules: tuple[Rule, ...]
    version: str = ""
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Source:
    name: str
    text: str


def _bundled(*parts: str) -> _Source:
    path = resources.files("archgate").joinpath("data", *parts)
    return _Source("/".join(parts), path.read_text())


def _bundled_rule_files() -> list[_Source]:
    folder = resources.files("archgate").joinpath("data", "rules")
    names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".yaml"))
    return [_bundled("rules", name) for name in names]


def _file(path: Path) -> _Source:
    try:
        return _Source(str(path), Path(path).read_text())
    except FileNotFoundError as exc:
        raise KnowledgeBaseError(f"file not found: {path}") from exc


def _parse(source: _Source) -> dict[str, Any]:
    try:
        data = yaml.safe_load(source.text)
    except yaml.YAMLError as exc:
        raise KnowledgeBaseError(f"{source.name}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise KnowledgeBaseError(f"{source.name}: expected a mapping at the top level")
    return data


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def load_catalog(path: Path | None = None) -> Catalog:
    return _build_catalog(_file(path) if path else _bundled("components.yaml"))


def _build_catalog(source: _Source) -> Catalog:
    data = _parse(source)
    components: dict[str, Component] = {}
    for cid, spec in (data.get("components") or {}).items():
        try:
            components[cid] = Component(
                id=cid,
                label=spec["label"],
                kind=spec["kind"],
                group=spec.get("group"),
                consistency=spec.get("consistency"),
                replicated=bool(spec.get("replicated", False)),
                requires_any=_as_tuple(spec.get("requires_any")),
                tradeoff=spec.get("tradeoff", ""),
            )
        except (KeyError, TypeError) as exc:
            raise KnowledgeBaseError(f"component {cid!r} is missing {exc}") from exc

    def check_ids(ids: tuple[str, ...], where: str) -> None:
        unknown = [cid for cid in ids if cid not in components]
        if unknown:
            raise KnowledgeBaseError(f"{where} references unknown components: {unknown}")

    for comp in components.values():
        check_ids(comp.requires_any, f"component {comp.id!r}")

    slots = {name: _as_tuple(ids) for name, ids in (data.get("required_slots") or {}).items()}
    for name, ids in slots.items():
        check_ids(ids, f"slot {name!r}")

    edges = []
    for i, spec in enumerate(data.get("edges") or []):
        edge = Edge(
            sources=_as_tuple(spec.get("from")),
            targets=_as_tuple(spec.get("to")),
            mode=spec.get("mode", "all"),
            style=spec.get("style", "solid"),
        )
        if edge.mode not in ("all", "first") or edge.style not in ("solid", "dotted"):
            raise KnowledgeBaseError(f"edge #{i} has an invalid mode or style")
        check_ids(edge.sources + edge.targets, f"edge #{i}")
        edges.append(edge)

    settings: dict[str, SettingSpec] = {}
    for key, spec in (data.get("settings") or {}).items():
        setting = SettingSpec(
            key=key,
            applies_to=_as_tuple(spec.get("applies_to")),
            values=_as_tuple(spec.get("values")),
            description=spec.get("description", ""),
        )
        if not setting.applies_to or not setting.values:
            raise KnowledgeBaseError(f"setting {key!r} needs applies_to and values")
        check_ids(setting.applies_to, f"setting {key!r}")
        settings[key] = setting

    groups: dict[str, list[str]] = {}
    for comp in components.values():
        if comp.group:
            groups.setdefault(comp.group, []).append(comp.id)

    return Catalog(
        components=components,
        required_slots=slots,
        edges=tuple(edges),
        groups={g: tuple(ids) for g, ids in groups.items()},
        settings=settings,
    )


def _check_condition(cond: Any, where: str) -> None:
    if not isinstance(cond, dict):
        raise KnowledgeBaseError(f"{where}: condition must be a mapping")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list):
            raise KnowledgeBaseError(f"{where}: '{key}' must be a list")
        for sub in cond[key]:
            _check_condition(sub, where)
    elif "not" in cond:
        _check_condition(cond["not"], where)
    else:
        if cond.get("field") not in FACT_FIELDS:
            raise KnowledgeBaseError(f"{where}: unknown field {cond.get('field')!r}")
        if cond.get("op") not in OPERATORS:
            raise KnowledgeBaseError(f"{where}: unknown operator {cond.get('op')!r}")
        if "value" not in cond:
            raise KnowledgeBaseError(f"{where}: condition is missing 'value'")


def _build_rule(spec: dict[str, Any], catalog: Catalog, origin: str, source: str) -> Rule:
    rid = spec.get("id")
    then = spec.get("then") or {}
    raw_settings = then.get("set") or {}
    if not isinstance(raw_settings, dict):
        raise KnowledgeBaseError(f"rule {rid}: 'set' must be a mapping")

    rule = Rule(
        id=rid,
        name=spec.get("name", rid),
        priority=int(spec.get("priority", 50)),
        when=spec.get("when") or {"all": []},
        add=_as_tuple(then.get("add")),
        remove=_as_tuple(then.get("remove")),
        why=spec.get("why", ""),
        covers=_as_tuple(spec.get("covers")),
        settings=tuple((str(k), str(v)) for k, v in raw_settings.items()),
        origin=origin,
        source=source,
    )
    _check_condition(rule.when, f"rule {rid}")
    unknown = [c for c in rule.add + rule.remove if c not in catalog]
    if unknown:
        raise KnowledgeBaseError(f"rule {rid} references unknown components: {unknown}")
    for key, value in rule.settings:
        spec_ = catalog.settings.get(key)
        if spec_ is None:
            raise KnowledgeBaseError(f"rule {rid} sets unknown setting {key!r}")
        if value not in spec_.values:
            raise KnowledgeBaseError(f"rule {rid}: {value!r} is not a valid {key}")
    if not (rule.add or rule.remove or rule.settings):
        raise KnowledgeBaseError(f"rule {rid} has no effect")
    if not rule.why.strip():
        raise KnowledgeBaseError(f"rule {rid} needs a 'why'")
    return rule


def _rules_from(sources: list[_Source], catalog: Catalog, origin: str) -> list[Rule]:
    rules = []
    for source in sources:
        for spec in _parse(source).get("rules") or []:
            rules.append(_build_rule(spec, catalog, origin, source.name))
    return rules


def load_rules(catalog: Catalog, path: Path | None = None) -> tuple[Rule, ...]:
    sources = [_file(path)] if path else _bundled_rule_files()
    rules = _rules_from(sources, catalog, ORIGIN_BUNDLED)
    _check_unique(rules)
    return tuple(rules)


def _check_unique(rules: list[Rule]) -> None:
    seen: dict[str, str] = {}
    for rule in rules:
        if not rule.id:
            raise KnowledgeBaseError(f"a rule in {rule.source} has no id")
        if rule.id in seen:
            raise KnowledgeBaseError(
                f"rule id {rule.id} is defined in both {seen[rule.id]} and {rule.source}"
            )
        seen[rule.id] = rule.source


def load_knowledge_base(
    components_path: Path | None = None,
    rules_path: Path | None = None,
    learned_path: Path | None = None,
) -> KnowledgeBase:
    """Bundled knowledge plus an optional file of rules learned from approved runs."""
    catalog_source = _file(components_path) if components_path else _bundled("components.yaml")
    catalog = _build_catalog(catalog_source)

    rule_sources = [_file(rules_path)] if rules_path else _bundled_rule_files()
    rules = _rules_from(rule_sources, catalog, ORIGIN_BUNDLED)

    learned_sources = []
    if learned_path is not None and Path(learned_path).exists():
        learned_sources = [_file(learned_path)]
        rules += _rules_from(learned_sources, catalog, ORIGIN_LEARNED)
    _check_unique(rules)

    all_sources = [catalog_source, *rule_sources, *learned_sources]
    digest = hashlib.sha256()
    for source in all_sources:
        digest.update(source.name.encode() + b"\0" + source.text.encode() + b"\0")

    return KnowledgeBase(
        catalog=catalog,
        rules=tuple(rules),
        version=digest.hexdigest()[:12],
        sources=tuple(s.name for s in all_sources),
    )
