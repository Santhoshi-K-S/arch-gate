from __future__ import annotations

from typing import Any

import pytest

from archgate.errors import LLMError
from archgate.knowledge import load_knowledge_base
from archgate.requirements import Requirements


@pytest.fixture(scope="session")
def kb():
    return load_knowledge_base()


@pytest.fixture
def make_req():
    def build(**overrides: Any) -> Requirements:
        base = {
            "name": "Test App",
            "daily_active_users": 100_000,
            "requests_per_user_per_day": 20,
            "read_write_ratio": 5,
            "avg_write_kb": 1,
            "latency_p99_ms": 300,
            "availability": 99.5,
            "consistency": "strong",
        }
        base.update(overrides)
        return Requirements.from_dict(base)

    return build


class FakeLLM:
    """Scripted stand-in for a model. Each call pops the next canned response."""

    name = "fake"

    def __init__(self, json_replies: list[Any] | None = None, text: str = "- looks fine"):
        self.json_replies = list(json_replies or [])
        self.text = text
        self.prompts: list[str] = []

    def complete_json(self, system: str, prompt: str, schema: dict) -> dict:
        self.prompts.append(prompt)
        if not self.json_replies:
            raise LLMError("no scripted reply left")
        reply = self.json_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def complete_text(self, system: str, prompt: str) -> str:
        return self.text


@pytest.fixture
def fake_llm():
    return FakeLLM
