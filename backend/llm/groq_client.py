"""
backend.llm.groq_client
=======================

Real LLM provider: **Groq** (https://groq.com) — free-tier, fast, and
OpenAI-compatible, so integration is a single HTTP call.

Notes
-----
* The API key comes from ``GROQ_API_KEY`` in ``.env``.
* Calls are retried once on rate-limit / transient server errors.
* All higher layers see only :class:`BaseLLMClient`.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

import httpx

from ..config import Settings
from .base import BaseLLMClient, LLMError, LLMMessage, LLMResponse


class GroqClient(BaseLLMClient):
    """Async Groq chat-completions client."""

    provider = "groq"

    def __init__(self, settings: Settings, *, model: Optional[str] = None) -> None:
        self._settings = settings
        # `model` lets the factory build one client for orchestrator
        # reasoning and another (cheaper) one for worker agents.
        self._model = model or settings.groq_model_orchestrator
        if not settings.groq_api_key:
            raise LLMError("GROQ_API_KEY is empty — cannot create a GroqClient")

    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": self._settings.llm_max_tokens if max_tokens is None else max_tokens,
        }
        if json_mode:
            # Groq supports OpenAI-style JSON mode.
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._settings.groq_base_url.rstrip('/')}/chat/completions"

        last_error: Optional[Exception] = None
        started = time.perf_counter()
        # One immediate retry covers the common transient cases
        # (429 rate limit, 500/502/503 hiccups).
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, json=payload, headers=headers)

                if response.status_code in (429, 500, 502, 503) and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                if response.status_code >= 400:
                    raise LLMError(
                        f"Groq API error {response.status_code}: {response.text[:400]}"
                    )

                data = response.json()
                content = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage", {}) or {}
                return LLMResponse(
                    content=content,
                    model=data.get("model", self._model),
                    usage={
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    latency_ms=(time.perf_counter() - started) * 1000,
                    provider=self.provider,
                    mocked=False,
                )
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1.0)

        raise LLMError(f"Groq call failed after retry: {last_error}")
