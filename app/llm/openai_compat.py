"""One provider implementation that speaks the OpenAI Chat Completions dialect.

Groq, OpenRouter, Mistral, NVIDIA, Cerebras, Cloudflare Workers AI, Hugging Face
router, Ollama, LM Studio, vLLM, LiteLLM and Gemini's compatibility endpoint all
implement this same shape, so a single well-tested code path covers them all.

Because model ids retire constantly, the client can run in two modes:

``model="auto"``
    Ask the provider's ``/models`` endpoint and rank what it actually serves
    (``app.llm.model_picker``). Nothing stale to break.
explicit ``model="..."``
    Used first. If the provider says the model is gone, the client either
    substitutes the next best model (``auto_substitute=True``, the default) or
    raises an error containing the provider's own message plus "did you mean"
    suggestions.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.llm.base import ModelResponse, ProviderError, ToolCall
from app.llm.model_picker import (
    DEFAULT_ATTEMPTS,
    looks_like_model_error,
    rank_models,
    suggest_models,
)


class OpenAICompatProvider:
    """A chat provider bound to one endpoint + model (or ``auto``)."""

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
        auto_substitute: bool = True,
        max_model_attempts: int = DEFAULT_ATTEMPTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.model = model or "auto"
        self.api_key = api_key
        self.auth_scheme = auth_scheme
        self.auth_header = auth_header
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})
        self.timeout = timeout
        self.key_url = key_url
        self.models_url = models_url
        self.auto_substitute = auto_substitute
        self.max_model_attempts = max(1, int(max_model_attempts))
        self._transport = transport
        self._entries: list[dict[str, Any]] | None = None
        self.active_model: str | None = None if self.is_auto else self.model

    # ------------------------------------------------------------- helpers
    @property
    def is_auto(self) -> bool:
        return self.model.strip().lower() in ("", "auto")

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

    @staticmethod
    def _provider_message(response: httpx.Response) -> str:
        """Extract the provider's own error text (much more useful than a status)."""
        try:
            data = response.json()
        except ValueError:
            return (response.text or "").strip()[:400]
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error.get("detail") or error)[:400]
            if isinstance(error, str):
                return error[:400]
            for key in ("detail", "message"):
                if data.get(key):
                    return str(data[key])[:400]
        return str(data)[:400]

    def _hint_for(self, status: int, body: str, retry_after: str | None = None) -> str | None:
        if status in (401, 403):
            return f"API key rejected. Create/rotate it at {self.key_url or 'your provider dashboard'}."
        if status == 402:
            return "Provider says the account is out of credits/quota."
        if looks_like_model_error(status, body):
            return (
                f"The model '{self.active_model or self.model}' is not available to this key "
                f"(retired, or no endpoints behind it). Set the model to 'auto' (or "
                f"LLM_MODEL=auto) to let the backend pick a live one, or copy a current id "
                f"from {self.models_url or 'the provider model list'}."
            )
        if status == 404:
            return (
                f"Endpoint not found at {self.base_url}/chat/completions. Check the base URL "
                "(OpenAI-style endpoints end with /v1; Gemini uses /v1beta/openai)."
            )
        if status == 429:
            retry = f" Provider asks to retry after {retry_after}s." if retry_after else ""
            return (
                "Rate limited (free tiers are tight). Retry later, or add LLM_FALLBACKS."
                + retry
            )
        if status == 400 and "tool" in body.lower():
            return (
                "The request was rejected because of tool calling: pick a model that "
                "supports function calling (auto-selection prefers those)."
            )
        return None

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.is_success:
            return
        status = response.status_code
        body = response.text[:1500]
        provider_message = self._provider_message(response)
        raise ProviderError(
            self.name,
            f"{action} failed with HTTP {status}: {provider_message}" if provider_message else f"{action} failed with HTTP {status}",
            status=status,
            body=body,
            hint=self._hint_for(status, body, response.headers.get("retry-after")),
        )

    # --------------------------------------------------------- model list
    async def _fetch_entries(self) -> list[dict[str, Any]]:
        """``/models`` normalised to ``[{"id": ..., "tools": bool|None}]``."""
        if self._entries is not None:
            return self._entries
        try:
            async with self._client() as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"network error listing models: {type(exc).__name__}: {exc}"
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
        if isinstance(entries, dict):
            entries = list(entries.values())

        normalised: list[dict[str, Any]] = []
        for entry in entries or []:
            if isinstance(entry, str):
                normalised.append({"id": entry, "tools": None})
                continue
            if not isinstance(entry, dict):
                continue
            model_id = (
                entry.get("id")
                or entry.get("name")
                or entry.get("model")
                or entry.get("slug")
            )
            if not isinstance(model_id, str) or not model_id:
                continue
            supported = entry.get("supported_parameters")
            tools: bool | None = None
            if isinstance(supported, list):
                tools = "tools" in supported or "tool_choice" in supported
            normalised.append({"id": model_id, "tools": tools})
        self._entries = normalised
        return normalised

    async def list_models(self) -> list[str]:
        return sorted({entry["id"] for entry in await self._fetch_entries()})

    async def ranked_models(self, *, require_tools: bool = False) -> list[str]:
        entries = await self._fetch_entries()
        ids = [entry["id"] for entry in entries]
        if self.name == "gemini":
            # Gemini lists "models/gemini-…"; the chat API takes "gemini-…".
            ids = [model[len("models/"):] if model.startswith("models/") else model for model in ids]
        tool_ids = {entry["id"] for entry in entries if entry.get("tools")}
        ranked = rank_models(
            self.name,
            ids,
            require_tools=require_tools,
            tool_capable=tool_ids or None,
        )
        if not ranked and require_tools:
            ranked = rank_models(self.name, ids)
        return ranked

    async def _candidates(self, *, require_tools: bool) -> list[str]:
        """Ranked models to try for ``model=auto`` (no network call for explicit ids)."""
        if not self.is_auto and not self.auto_substitute:
            return [self.model]
        if not self.is_auto:
            return [self.model]

        ranked = await self.ranked_models(require_tools=require_tools)
        if not ranked:
            raise ProviderError(
                self.name,
                "the provider lists no usable chat models for this key",
                hint=f"Check {self.models_url or 'the provider dashboard'} and set the model env explicitly.",
            )
        return ranked[: self.max_model_attempts]

    async def _alternatives(self, require_tools: bool, exclude: list[str]) -> list[str]:
        """Live replacements for a model the provider refused (retired / no endpoints)."""
        ranked = await self.ranked_models(require_tools=require_tools)
        return [model for model in ranked if model not in exclude]

    async def _enrich(self, error: ProviderError, attempts: list[dict[str, Any]]) -> ProviderError:
        """Add live model suggestions to a model-related failure."""
        if not looks_like_model_error(error.status, error.body or ""):
            return error
        try:
            ranked = await self.ranked_models()
        except ProviderError:
            ranked = [entry["id"] for entry in (self._entries or [])]
        if not ranked:
            return error
        suggestions = suggest_models(self.active_model or self.model, ranked, limit=3)
        if suggestions:
            extra = "; live options: " + ", ".join(suggestions)
            error.hint = (error.hint or "") + extra
        if attempts:
            error.hint = (error.hint or "") + (
                f" (also tried: {', '.join(a.get('model', '?') for a in attempts)})"
            )
        return error

    # -------------------------------------------------------------- public
    async def _call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None,
        max_tokens: int | None,
        tool_choice: str | None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": model,
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

        self.active_model = model
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
            model=data.get("model") or model,
            provider=self.name,
            usage=data.get("usage") or {},
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = "auto",
    ) -> ModelResponse:
        require_tools = bool(tools)
        # ``auto`` resolves up front; an explicit id is tried first with no extra
        # /models request, and only rotated away from if the provider refuses it.
        order = await self._candidates(require_tools=require_tools) if self.is_auto else [self.model]

        failures: list[dict[str, Any]] = []
        last_error: ProviderError | None = None
        attempts = 0
        index = 0

        while index < len(order) and attempts < self.max_model_attempts:
            model = order[index]
            index += 1
            attempts += 1
            try:
                return await self._call(
                    model,
                    messages,
                    tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tool_choice=tool_choice,
                )
            except ProviderError as exc:
                last_error = exc
                if not looks_like_model_error(exc.status, exc.body or ""):
                    raise await self._enrich(exc, failures)
                failures.append({"model": model, "status": exc.status})
                if not self.auto_substitute:
                    break
                if index >= len(order) and attempts < self.max_model_attempts:
                    try:
                        order.extend(
                            await self._alternatives(require_tools, exclude=list(order))
                        )
                    except ProviderError:
                        break

        assert last_error is not None
        raise await self._enrich(last_error, failures[:-1])

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
                "model": self.active_model or self.model,
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
