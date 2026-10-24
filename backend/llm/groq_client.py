"""
backend.llm.groq_client
=======================

Real LLM provider: **Groq** (https://groq.com) — free-tier, fast, and
OpenAI-compatible, so integration is a single HTTP call.

Notes
-----
* The API key comes from ``GROQ_API_KEY`` in ``.env``.
* Calls are retried once on rate-limit / transient server errors.
* Deprecated Groq free/developer-tier model IDs (such as
  ``llama-3.3-70b-versatile`` or ``llama-3.1-8b-instant``) automatically
  migrate on ``404 model_not_found`` to Groq's recommended replacements
  so stale ``.env`` files keep working seamlessly.
* All higher layers see only :class:`BaseLLMClient`.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Dict, List, Optional

import httpx

from ..config import Settings
from .base import BaseLLMClient, LLMError, LLMMessage, LLMResponse

# Official Groq model replacements (https://console.groq.com/docs/deprecations)
DEPRECATED_MODEL_REPLACEMENTS: Dict[str, str] = {
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama-3.3-70b-specdec": "openai/gpt-oss-120b",
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
    "llama3-70b-8192": "openai/gpt-oss-120b",
    "llama3-8b-8192": "openai/gpt-oss-20b",
    "deepseek-r1-distill-llama-70b": "openai/gpt-oss-120b",
    "qwen/qwen3-32b": "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b": "qwen/qwen3.8-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct": "openai/gpt-oss-120b",
}

_THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def _strip_raw_thinking(text: str) -> str:
    """Strip a leading ``<think>…</think>`` block if a model emits raw CoT."""
    return _THINK_BLOCK_RE.sub("", text)


class GroqClient(BaseLLMClient):
    """Async Groq chat-completions client."""

    provider = "groq"

    def __init__(self, settings: Settings, *, model: Optional[str] = None) -> None:
        self._settings = settings
        # `model` lets the factory build one client for orchestrator
        # reasoning and another (faster/cheaper) one for worker agents.
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
        completion_budget = (
            self._settings.llm_max_tokens if max_tokens is None else max_tokens
        )
        headers = {
            "Authorization": f"Bearer {self._settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._settings.groq_base_url.rstrip('/')}/chat/completions"

        last_error: Optional[Exception] = None
        started = time.perf_counter()
        # Up to 3 attempts allows one retired-model migration + one transient retry.
        for attempt in range(3):
            payload = {
                "model": self._model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "temperature": (
                    self._settings.llm_temperature if temperature is None else temperature
                ),
                "max_completion_tokens": completion_budget,
            }
            if json_mode:
                # Groq supports OpenAI-style JSON mode.
                payload["response_format"] = {"type": "json_object"}

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, json=payload, headers=headers)

                if response.status_code in (429, 500, 502, 503) and attempt < 2:
                    await asyncio.sleep(1.5)
                    continue

                if response.status_code == 404 and "model_not_found" in response.text:
                    replacement = DEPRECATED_MODEL_REPLACEMENTS.get(self._model)
                    if replacement and replacement != self._model:
                        self._model = replacement
                        continue
                    raise LLMError(
                        f"Groq model '{self._model}' was not found or is retired for "
                        "your account tier. Update GROQ_MODEL_ORCHESTRATOR / "
                        "GROQ_MODEL_AGENT in .env (for example 'openai/gpt-oss-120b' "
                        "or 'openai/gpt-oss-20b'; see https://console.groq.com/docs/models)."
                    )

                if response.status_code >= 400:
                    raise LLMError(
                        f"Groq API error {response.status_code}: {response.text[:400]}"
                    )

                data = response.json()
                raw_content = data["choices"][0]["message"]["content"] or ""
                content = _strip_raw_thinking(raw_content)
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
                if attempt < 2:
                    await asyncio.sleep(1.0)

        raise LLMError(f"Groq call failed after retry: {last_error}")
