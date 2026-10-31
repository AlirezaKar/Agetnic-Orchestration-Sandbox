"""Provider-agnostic chat/message types."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """A model call failed (bad key, rate limit, unsupported tool calling, ...)."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.provider = provider
        self.message = message
        self.status = status
        self.body = body
        self.hint = hint
        super().__init__(f"[{provider}] {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "message": str(self),
            "hint": self.hint,
            "body": (self.body or "")[:600],
        }


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")

    @classmethod
    def from_openai(cls, raw: dict[str, Any]) -> "ToolCall":
        fn = raw.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                parsed = json.loads(args or "{}")
            except json.JSONDecodeError:
                parsed = {"_raw": args}
        elif isinstance(args, dict):
            parsed = args
        else:
            parsed = {}
        return cls(
            name=fn.get("name") or raw.get("name") or "unknown",
            arguments=parsed if isinstance(parsed, dict) else {"value": parsed},
            id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}",
        )

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class ModelResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str | None = None

    def to_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        return message

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [
                {"name": tc.name, "arguments": tc.arguments, "id": tc.id}
                for tc in self.tool_calls
            ],
            "model": self.model,
            "provider": self.provider,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
        }
