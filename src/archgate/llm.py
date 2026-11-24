from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from archgate.errors import LLMError

DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_OLLAMA_URL = "http://localhost:11434"


class LLMClient(Protocol):
    name: str

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        ...

    def complete_text(self, system: str, prompt: str) -> str:
        ...


class OllamaClient:
    """Talks to a local Ollama server. Uses only the standard library."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = f"ollama:{model}"

    def is_available(self) -> bool:
        try:
            tags = self._request("GET", "/api/tags", None, timeout=3)
        except LLMError:
            return False
        names = {m.get("name", "") for m in tags.get("models", [])}
        # "qwen2.5:3b" may be listed as-is, or "llama3.2" as "llama3.2:latest"
        return self.model in names or f"{self.model}:latest" in names

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        content = self._chat(system, prompt, fmt=schema)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {content[:200]!r}") from exc
        if not isinstance(data, dict):
            raise LLMError("model returned JSON that is not an object")
        return data

    def complete_text(self, system: str, prompt: str) -> str:
        return self._chat(system, prompt, fmt=None).strip()

    def _chat(self, system: str, prompt: str, fmt: dict[str, Any] | None) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0},
        }
        if fmt is not None:
            body["format"] = fmt
        reply = self._request("POST", "/api/chat", body, timeout=self.timeout)
        try:
            return reply["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected Ollama response: {str(reply)[:200]}") from exc

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise LLMError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"cannot reach Ollama at {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError("Ollama returned a non-JSON response") from exc
