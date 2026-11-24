from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

ACTORS = ("pipeline", "code", "rule", "validator", "llm", "human")


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    at: str
    state: str
    actor: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Trace:
    """Append-only record of every decision, so "why B instead of A?" has an answer."""

    events: list[TraceEvent] = field(default_factory=list)

    def record(self, state: str, actor: str, action: str, **detail: Any) -> TraceEvent:
        if actor not in ACTORS:
            raise ValueError(f"unknown actor {actor!r}")
        event = TraceEvent(
            seq=len(self.events) + 1,
            at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            state=state,
            actor=actor,
            action=action,
            detail=detail,
        )
        self.events.append(event)
        return event

    def by_actor(self, actor: str) -> list[TraceEvent]:
        return [e for e in self.events if e.actor == actor]

    @classmethod
    def from_dicts(cls, items: list[dict[str, Any]]) -> Trace:
        return cls([TraceEvent(**item) for item in items])
