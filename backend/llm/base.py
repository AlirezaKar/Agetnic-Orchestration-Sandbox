"""
backend.llm.base
================

Provider-agnostic LLM contract.

Both the real Groq client and the offline Mock client implement
:class:`BaseLLMClient`, so every higher layer (Manager, Planner, agents)
is written against one tiny interface and never cares which provider is
actually answering.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class LLMError(RuntimeError):
    """Raised when a provider call fails after retries."""


@dataclass
class LLMMessage:
    """One chat message. ``role`` is ``system`` | ``user`` | ``assistant``."""

    role: str
    content: str


@dataclass
class LLMResponse:
    """Uniform response envelope with light usage accounting."""

    content: str
    model: str = "unknown"
    usage: Dict[str, int] = field(default_factory=dict)  # prompt/completion/total tokens
    latency_ms: float = 0.0
    provider: str = "unknown"
    mocked: bool = False


class BaseLLMClient(abc.ABC):
    """
    Minimal chat-completion interface shared by all providers.

    Implementations must be safe to call concurrently (the parallel
    executor fans out agent calls with ``asyncio.gather``).
    """

    provider: str = "base"

    @abc.abstractmethod
    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """
        Run one chat completion.

        Parameters
        ----------
        messages:
            The full conversation (system prompt first).
        temperature / max_tokens:
            Per-call overrides of the global settings.
        json_mode:
            Hint that the caller expects a single JSON object back.
            Providers that support structured output may enforce it;
            others just receive an explicit instruction in the prompt.
        """

    # ── convenience helpers ─────────────────────────────────────
    async def complete_text(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str:
        """Shorthand for the very common ``system + single user`` call."""
        response = await self.complete(
            [LLMMessage("system", system), LLMMessage("user", user)],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        return response.content
