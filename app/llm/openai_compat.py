"""One provider implementation that speaks the OpenAI Chat Completions dialect.

Groq, OpenRouter, Mistral, NVIDIA, Cerebras, Cloudflare Workers AI, Hugging Face
router, Ollama, LM Studio, vLLM, LiteLLM and Gemini's compatibility endpoint all
implement this same shape, so a single well-tested code path covers them all.
Provider-specific quirks (auth header, extra headers, base URL, model ids) are
described by ``app.llm.presets``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.llm.base import ModelResponse, ProviderError, ToolCall


class OpenAICompatProvider:
    """A chat provider bound to one endpoint + model."""

    def __init__(
        self,
        *,
        name: str,
        label: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        auth_scheme: str = "bearer",
        auth_header: str = "Authorization",
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 60.0,
        key_url: str = "",
        models_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.auth_scheme = auth_scheme
        self.auth_header = auth_header
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})
        self.timeout = timeout
        self.key_url = key_url
        self.models_url = models_url
        self._transport = transport

    # ------------------------------------------------------------- helpers
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name} model={self.model}>"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.extra_headers,
        }
        if self.api_key and self.auth_scheme != "none":
            if self.auth_scheme == "bearer":
                headers[self.auth_header] = f"Bearer {self.api_key}"
            else:  # "raw"
                headers[self.auth_header] = self.api_key
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
        )

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        body = response.text[:1200]
        status = response.status_code
        hint = None
        if status in (401, 403):
            hint = f"API key rejected. Create/rotate it at {self.key_url or 'your provider dashboard'}."
        elif status == 402:
            hint = "Provider says the account is out of credits/quota."
        elif status == 404:
            hint = (
                "Model or path not found. Check the exact model id at "
                f"{self.models_url or 'the provider model list'} (GET /providers/{self.name}/models)."
            )
        elif status == 429:
            retry = response.headers.get("retry-after")
            hint = "Rate limited (free tiers are tight). Retry later" + (
                f" after {retry}s." if retry else "."
            )
        elif status == 400 and "tool" in body.lower():
            hint = (
                "The model rejected tool calling. Pick a model that supports function "
                "calling (most Llama/Qwen/Gemini ones do; tiny 1-3B models often do not)."
            )
        raise ProviderError(
            self.name,
            f"{action} failed with HTTP {status}",
            status=status,
            body=body,
            hint=hint,
        )

    # -------------------------------------------------------------- public
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = "auto",
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        payload.update(self.extra_body)

        started = time.perf_counter()
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                f"network error talking to {self.base_url}: {type(exc).__name__}: {exc}",
                hint="Check outbound network access / the base URL. For local runtimes "
                "make sure the server is actually running.",
            ) from exc

        self._raise_for_status(response, "chat/completions")
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
        except (ValueError, AttributeError, IndexError) as exc:
            raise ProviderError(
                self.name,
                "provider returned a response that is not OpenAI-shaped",
                status=response.status_code,
                body=response.text[:800],
            ) from exc

        raw_calls = message.get("tool_calls") or []
        tool_calls = [ToolCall.from_openai(tc) for tc in raw_calls if isinstance(tc, dict)]

        content = message.get("content")
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return ModelResponse(
            content=(content or "").strip(),
            tool_calls=tool_calls,
            model=data.get("model") or self.model,
            provider=self.name,
            usage=data.get("usage") or {},
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
        )

    async def list_models(self) -> list[str]:
        try:
            async with self._client() as client:
                response = await client.get(
                    f"{self.base_url}/models", headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"network error listing models: {exc}"
            ) from exc
        self._raise_for_status(response, "models")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name, "model list was not JSON", body=response.text[:400]
            ) from exc

        entries: Any = None
        if isinstance(data, dict):
            entries = data.get("data") or data.get("result") or data.get("models")
        elif isinstance(data, list):
            entries = data
        if isinstance(entries, dict):  # e.g. {"models": {"id": {...}}}
            entries = list(entries.values())

        ids: list[str] = []
        for entry in entries or []:
            if isinstance(entry, str):
                ids.append(entry)
            elif isinstance(entry, dict):
                model_id = (
                    entry.get("id")
                    or entry.get("name")
                    or entry.get("model")
                    or entry.get("slug")
                )
                if isinstance(model_id, str) and model_id:
                    ids.append(model_id)
        return sorted(set(ids))

    async def ping(self) -> dict[str, Any]:
        """Cheap end-to-end check: does the key + model actually answer?"""
        started = time.perf_counter()
        try:
            response = await self.complete(
                [{"role": "user", "content": "Reply with exactly: pong"}],
                temperature=0,
                max_tokens=16,
            )
        except ProviderError as exc:
            return {
                "ok": False,
                "provider": self.name,
                "model": self.model,
                "error": exc.to_dict(),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        return {
            "ok": True,
            "provider": self.name,
            "model": response.model,
            "reply": response.content[:200],
            "latency_ms": response.latency_ms,
        }
