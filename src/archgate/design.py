from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Who put a component into the design. Shown to reviewers next to every component.
SOURCE_RULE = "rule"
SOURCE_VALIDATOR = "validator"
SOURCE_LLM = "llm"


@dataclass
class Reason:
    source: str
    ref: str
    why: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "ref": self.ref, "why": self.why}


@dataclass
class Choice:
    component: str
    reasons: list[Reason] = field(default_factory=list)

    @property
    def sources(self) -> set[str]:
        return {r.source for r in self.reasons}

    @property
    def deterministic(self) -> bool:
        return SOURCE_LLM not in self.sources


@dataclass
class Setting:
    key: str
    value: str
    reason: Reason

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, **self.reason.to_dict()}


@dataclass
class Design:
    choices: dict[str, Choice] = field(default_factory=dict)
    settings: dict[str, Setting] = field(default_factory=dict)

    def __contains__(self, component: str) -> bool:
        return component in self.choices

    @property
    def components(self) -> set[str]:
        return set(self.choices)

    def add(self, component: str, source: str, ref: str, why: str) -> bool:
        """Returns True if the component is new to the design."""
        is_new = component not in self.choices
        choice = self.choices.setdefault(component, Choice(component))
        choice.reasons.append(Reason(source, ref, why))
        return is_new

    def remove(self, component: str) -> bool:
        return self.choices.pop(component, None) is not None

    def set(self, key: str, value: str, source: str, ref: str, why: str) -> None:
        self.settings[key] = Setting(key, value, Reason(source, ref, why))

    def components_dict(self, order: list[str] | None = None) -> dict[str, Any]:
        ids = order if order is not None else sorted(self.choices)
        return {
            cid: {
                "deterministic": self.choices[cid].deterministic,
                "reasons": [r.to_dict() for r in self.choices[cid].reasons],
            }
            for cid in ids
        }

    def settings_dict(self) -> dict[str, Any]:
        return {key: self.settings[key].to_dict() for key in sorted(self.settings)}

    @classmethod
    def from_dicts(
        cls, components: dict[str, Any], settings: dict[str, Any] | None = None
    ) -> Design:
        design = cls()
        for cid, spec in components.items():
            design.choices[cid] = Choice(cid, [Reason(**r) for r in spec.get("reasons", [])])
        for key, spec in (settings or {}).items():
            design.set(key, spec["value"], spec["source"], spec["ref"], spec["why"])
        return design
