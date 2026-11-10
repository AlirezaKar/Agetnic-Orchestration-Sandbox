"""The ARAS microservices — the only backends this app talks to.

One service per capability (see README §"ARAS microservices"); this module is
the only place that knows their URLs, auth and JSON shapes. Each one fills a
port of the orchestrator, and nothing else is wired in beside them:

* router :8101  — the model. :class:`ArasModel` is the orchestrator's
  ``ModelChain`` port: every planner / agent / evaluator / manager call is one
  ``POST /api/v1/chat/completions/`` (OpenAI dialect, trailing slash included)
* web    :8108  — web search. :class:`ArasSearch` is the orchestrator's
  ``SearchChain`` port: the ``web_search`` tool of the search agent
* ocr    :8102  — the PDF reader injected into ``files.extract``
* retrieval :8105 + ingestion :8104 — :class:`ArasKnowledge`, the orchestrator's
  ``KnowledgeStore`` port (RAG for the document agent)
* data   :8107  — :class:`ArasDataBackend`, the orchestrator's ``DataBackend``
  port (natural-language analytics for the data agent)
* vector :8103  — health/diagnostics only; ingestion and retrieval are the
  right doors for normal orchestrator work

Port 8106 (the remote orchestrator) is never called: this app *is* the
orchestrator.

Auth is ``Authorization: Bearer <token>`` per service, each token falling back
to the shared ``ARAS_TOKEN`` (``app/config.py``). The data service additionally
wants ``X-Tenant`` and ``X-Owner-External-Id`` on every call.

Every failure is one exception type, :class:`ArasError`, which names the
service, the HTTP status and a hint about what to fix — so a broken run always
says *which* microservice broke. No secret ever leaves this process: errors
carry status + the service's ``error``/``detail`` text, never the token.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.config import Settings, trust_env_for

#: The name every reply and every error carries in its ``provider`` field.
PROVIDER = "aras"

#: service → the unauthenticated health path (they differ per service)
HEALTH_PATHS: dict[str, str] = {
    "router": "/health/",  # under /api/v1 (part of the endpoint already)
    "ocr": "/api/v1/health/",
    "vector": "/health/",
    "ingestion": "/health/",
    "retrieval": "/health/",
    "data": "/v1/health/",
    "web": "/v1/health/",
}


def config_hint(service: str, missing: list[str]) -> str:
    """Where to write each missing value — one sentence, no guessing."""
    notes: list[str] = []
    for env in missing:
        if env == "ARAS_BASE_URL":
            notes.append(f"ARAS_BASE_URL (or ARAS_{service.upper()}_URL)")
        elif env.endswith("_TOKEN"):
            notes.append(f"{env} (or the shared ARAS_TOKEN)")
        elif env == "ARAS_MODEL":
            notes.append("ARAS_MODEL (a router model id — GET /aras/models lists them)")
        else:
            notes.append(env)
    return "set " + ", ".join(notes) + " in .env, then restart" if notes else ""


class ArasError(RuntimeError):
    """One ARAS call failed (network, auth, 4xx/5xx, bad JSON).

    The single failure type of this app's backends: whichever port raised it
    (model, search, knowledge, data, OCR), the message names the service and
    ``hint`` says what to check first.
    """

    def __init__(self, service: str, message: str, *, status: int | None = None, hint: str = ""):
        self.service = service
        self.status = status
        self.hint = hint
        super().__init__(f"[aras:{service}] {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "service": self.service,
            "status": self.status,
            "message": str(self),
            "hint": self.hint,
        }


class ArasNotConfigured(ArasError):
    """A service cannot be called yet: ``missing`` lists the env vars to fill."""

    def __init__(self, service: str, missing: list[str]):
        self.missing = list(missing)
        detail = "not configured"
        if self.missing:
            detail += " — missing " + ", ".join(self.missing)
        super().__init__(service, detail, hint=config_hint(service, self.missing))

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "missing": self.missing}


# -------------------------------------------------------------- reply types
# What the ports hand back. The orchestrator never imports these: it reads the
# attributes (``content``, ``tool_calls``, ``results``, ...), which is why any
# object with the same shape — a test fake, another host's client — fits.
@dataclass
class ToolCall:
    """One function call the model asked for (OpenAI ``tool_calls`` entry)."""

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
    """One reply from the router: text and/or tool calls, plus who answered."""

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


@dataclass
class SearchResult:
    """One web hit: where it is, what it says, which engine found it."""

    title: str
    url: str
    snippet: str = ""
    source: str = ""  # "aras:<engine>" — the engine behind the web service

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_tool_output(self, max_chars: int = 600) -> dict[str, Any]:
        """Compact shape handed to the model."""
        snippet = (self.snippet or "").strip()
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars] + "…"
        return {"title": self.title, "url": self.url, "snippet": snippet}


@dataclass
class SearchResponse:
    """One web search: the query, its hits, who answered and how long it took."""

    query: str
    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "results": [r.to_dict() for r in self.results],
            "count": len(self.results),
            "latency_ms": self.latency_ms,
        }


# ------------------------------------------------------------------- client
class ArasClient:
    """Base: one service, one base URL, one bearer token."""

    service = "base"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.base_url = settings.aras_endpoint(self.service)
        self.token = getattr(settings, f"aras_{self.service}_token", None)
        self.timeout = settings.aras_timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _headers(self, **extra: str) -> dict[str, str]:
        headers = {"Accept": "application/json", **extra}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        # trust_env=False on loopback: a VPN/system proxy must never answer
        # for the SSH tunnel (it replies 503 for every service at once).
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=False,
            trust_env=trust_env_for(self.base_url or ""),
        )

    def _require(self) -> None:
        if self.configured:
            return
        missing: list[str] = []
        if not self.base_url:
            missing.append("ARAS_BASE_URL")
        if not self.token:
            missing.append(f"ARAS_{self.service.upper()}_TOKEN")
        raise ArasNotConfigured(self.service, missing)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """The service's own words for a failure (far more useful than a status)."""
        try:
            body = response.json()
        except ValueError:
            return " ".join((response.text or "").split())[:300]
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):  # OpenAI style: {"error": {"message": ...}}
                return str(error.get("message") or error.get("detail") or error)[:300]
            for key in ("error", "detail", "message"):
                if body.get(key):
                    return str(body[key])[:300]
            return ""
        return str(body)[:300]

    def _hint_for(self, status: int, detail: str) -> str:
        """What to check first for an HTTP failure; services add their own."""
        if status == 403:
            return (
                f"the token was read but is not allowed to do this — check that "
                f"ARAS_{self.service.upper()}_TOKEN is this service's token and still active"
            )
        if status >= 500:
            return (
                "the service answered with a server error — GET /aras (or "
                "python scripts/aras_smoke.py) shows whether it is this service or the tunnel"
            )
        return ""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self._require()
        url = f"{self.base_url}{path}"
        try:
            async with self._client() as client:
                response = await client.request(
                    method,
                    url,
                    json=json,
                    data=data,
                    files=files,
                    params=params,
                    headers=self._headers(**(headers or {})),
                )
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            # connected, then silence: the service is there but slow — a
            # different fix from a closed tunnel, so it gets its own words
            raise ArasError(
                self.service,
                f"no answer from {url} within {self.timeout:g}s ({type(exc).__name__})",
                hint="the connection opened, so the tunnel is up — the service is slow or "
                "stuck. Raise ARAS_TIMEOUT (model calls can take a while), then retry",
            ) from exc
        except httpx.HTTPError as exc:
            raise ArasError(
                self.service,
                f"network error talking to {url}: {type(exc).__name__}: {exc}",
                hint="Is the SSH tunnel to the ARAS server open? "
                "(ssh -L <port>:127.0.0.1:<port> ... — see the ARAS access README)",
            ) from exc
        if response.status_code == 401:
            raise ArasError(
                self.service,
                "401 unauthorized — the token was rejected",
                status=401,
                hint=f"check ARAS_{self.service.upper()}_TOKEN (or ARAS_TOKEN); the key "
                "may have been set inactive on the service's /admin/",
            )
        if 300 <= response.status_code < 400:
            # never followed: a redirected POST loses its body, so the service
            # would answer something that looks like success and is not
            raise ArasError(
                self.service,
                f"HTTP {response.status_code}: redirected to "
                f"{response.headers.get('location') or '(no location)'}",
                status=response.status_code,
                hint=f"the URL {url} is not the route itself — ARAS routes end with a "
                f"slash; check ARAS_BASE_URL / ARAS_{self.service.upper()}_URL",
            )
        if response.status_code >= 400:
            detail = self._error_detail(response)
            raise ArasError(
                self.service,
                f"HTTP {response.status_code}: {detail or 'request failed'}",
                status=response.status_code,
                hint=self._hint_for(response.status_code, detail),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ArasError(self.service, "the service returned a body that is not JSON") from exc

    async def health(self) -> dict[str, Any]:
        """The unauthenticated health probe, timed; never raises."""
        started = time.perf_counter()
        if not self.base_url:
            return {"service": self.service, "ok": False, "configured": False,
                    "error": "no URL configured"}
        url = f"{self.base_url}{HEALTH_PATHS[self.service]}"
        try:
            async with self._client() as client:
                response = await client.get(url)
            ok = response.status_code == 200
            if ok:
                detail = ""
            else:
                # the first line of the body names who actually answered —
                # a proxy's error page looks nothing like a Django service
                snippet = " ".join((response.text or "").split())[:160]
                detail = f"HTTP {response.status_code}" + (f" — {snippet}" if snippet else "")
        except httpx.HTTPError as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        return {
            "service": self.service,
            "ok": ok,
            "configured": self.configured,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            **({"error": detail} if detail else {}),
        }


# ------------------------------------------------------------------ services
class ArasRouterClient(ArasClient):
    """Router :8101 — the model gateway, speaking the OpenAI chat dialect.

    The endpoint already ends in ``/api/v1``. Routes carry a trailing slash:
    the service is a Django app, and a POST without it is redirected and loses
    its body.
    """

    service = "router"

    def _hint_for(self, status: int, detail: str) -> str:
        lowered = detail.lower()
        model = self.settings.aras_model_id or "(not set)"
        if status in (400, 404, 422) and "tool" in lowered:
            return (
                "the router refused the tool-calling request — the search agent needs a "
                "model that supports function calling; try another ARAS_MODEL "
                "(GET /aras/models lists the ids)"
            )
        if status in (400, 403, 404, 422) and "model" in lowered:
            return (
                f"the router refused ARAS_MODEL={model} — copy an id from GET /aras/models. "
                "Ids with the sharif- prefix are routed to Rayen; gapgpt-/openrouter-/"
                "deepseek- prefixes are rejected for this token"
            )
        if status == 404:
            return (
                "nothing answers at this route — ARAS_ROUTER_URL (when set) must end "
                f"with /api/v1, and ARAS_MODEL={model} must be an id from GET /aras/models"
            )
        if status == 429:
            return "the router is rate limiting this token — wait, then retry"
        return super()._hint_for(status, detail)

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /chat/completions/`` — returns the router's JSON untouched."""
        data = await self._request("POST", "/chat/completions/", json=payload)
        if not isinstance(data, dict):
            raise ArasError(
                self.service,
                "the router's reply is not a JSON object: " + str(data)[:200],
            )
        return data

    async def models(self) -> list[str]:
        """``GET /models/`` — the model ids the router lists, sorted."""
        payload = await self._request("GET", "/models/")
        entries: Any = payload
        if isinstance(payload, dict):
            entries = payload.get("data") or payload.get("models") or payload.get("results") or []
        ids: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str) and entry:
                ids.add(entry)
            elif isinstance(entry, dict):
                value = entry.get("id") or entry.get("name") or entry.get("model")
                if isinstance(value, str) and value:
                    ids.add(value)
        return sorted(ids)


class ArasOcrClient(ArasClient):
    """OCR :8102 — ``POST /api/v1/ocr/`` reads a PDF into text."""

    service = "ocr"

    async def extract_text(self, filename: str, data: bytes, *, language: str | None = None) -> dict[str, Any]:
        form: dict[str, Any] = {}
        if language:
            form["language"] = language
        payload = await self._request(
            "POST",
            "/api/v1/ocr/",
            data=form or None,
            files={"file": (filename, data, "application/pdf")},
        )
        return {
            "text": str(payload.get("text") or ""),
            "pages": payload.get("page_count"),
            "engine": str(payload.get("engine") or ""),
        }


class ArasIngestionClient(ArasClient):
    """Ingestion :8104 — ``POST /v1/ingest/`` embeds + indexes a document."""

    service = "ingestion"

    async def ingest(
        self,
        filename: str,
        data: bytes,
        *,
        collection: str,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/v1/ingest/",
            data={"collection": collection},
            files={"file": (filename, data, content_type)},
        )
        return {
            "success": bool(payload.get("success")),
            "document_id": payload.get("document_id"),
            "collection": payload.get("collection"),
            "chunks": payload.get("chunks"),
            "indexed_points": payload.get("indexed_points"),
        }


class ArasRetrievalClient(ArasClient):
    """Retrieval :8105 — ``POST /v1/search/`` searches an indexed collection."""

    service = "retrieval"

    async def search(
        self,
        query: str,
        *,
        collection: str,
        mode: str = "hybrid",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST",
            "/v1/search/",
            json={"query": query, "collection": collection, "mode": mode, "limit": limit},
        )
        if payload.get("success") is False:
            raise ArasError(self.service, str(payload.get("error") or "search failed"))
        hits = payload.get("results") or []
        return [
            {
                "text": str(hit.get("text") or ""),
                "source": str(hit.get("source_filename") or hit.get("document_id") or ""),
                "document_id": str(hit.get("document_id") or ""),
                "score": hit.get("score"),
                "pages": [hit.get("page_start"), hit.get("page_end")],
            }
            for hit in hits
            if isinstance(hit, dict)
        ]


class ArasVectorClient(ArasClient):
    """Vector store :8103 — diagnostics only (collections live here)."""

    service = "vector"

    async def collections(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/v1/collections/")
        return list(payload.get("collections") or [])


class ArasWebClient(ArasClient):
    """Web :8108 — public web search: the search agent's one tool."""

    service = "web"

    def _hint_for(self, status: int, detail: str) -> str:
        if status in (502, 503, 504):
            return (
                "the web service answered, but the search engines behind it timed out "
                "or are down — retry in a moment"
            )
        return super()._hint_for(status, detail)

    async def search(self, query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
        payload = await self._request(
            "POST", "/v1/search/", json={"query": query, "max_results": max_results}
        )
        return [
            {
                "title": str(hit.get("title") or ""),
                "url": str(hit.get("url") or ""),
                "snippet": str(hit.get("snippet") or ""),
                "engine": str(hit.get("engine") or ""),
            }
            for hit in payload.get("results") or []
            if isinstance(hit, dict)
        ]


class ArasDataClient(ArasClient):
    """Data :8107 — natural-language analytics over registered sources."""

    service = "data"

    def _owner_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.aras_tenant:
            headers["X-Tenant"] = str(self.settings.aras_tenant)
        if self.settings.aras_owner:
            headers["X-Owner-External-Id"] = str(self.settings.aras_owner)
        return headers

    async def sources(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/v1/data-sources/", headers=self._owner_headers())
        if isinstance(payload, list):
            return payload
        return list(payload.get("results") or payload.get("data") or [])

    async def query(self, question: str, *, data_source_id: str) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/v1/query/",
            json={"data_source_id": data_source_id, "question": question},
            headers=self._owner_headers(),
        )
        if payload.get("success") is False:
            raise ArasError(self.service, str(payload.get("error") or "query failed"))
        data = payload.get("data") or {}
        return {
            "answer": str(payload.get("answer") or ""),
            "queries": payload.get("queries") or [],
            "columns": data.get("columns") or [],
            "rows": data.get("rows") or [],
            "chart_spec": payload.get("chart_spec"),
        }


#: Every service this app may call, in port order (8101 … 8108, never 8106).
CLIENTS: dict[str, type[ArasClient]] = {
    "router": ArasRouterClient,
    "ocr": ArasOcrClient,
    "vector": ArasVectorClient,
    "ingestion": ArasIngestionClient,
    "retrieval": ArasRetrievalClient,
    "data": ArasDataClient,
    "web": ArasWebClient,
}


# ------------------------------------------------------------- port adapters
class ArasModel:
    """The orchestrator's ``ModelChain`` port: the ARAS router, and only it.

    No failover and no model guessing: ``ARAS_MODEL`` is the id that is sent,
    and when the router refuses it the error says so and the call stops. The
    object is falsy until the router is configured, which is how the
    orchestrator knows whether it has a model at all.
    """

    name = PROVIDER
    service = "router"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = ArasRouterClient(settings, transport=transport)
        self.model = settings.aras_model_id

    @property
    def missing(self) -> list[str]:
        """Env vars still needed before a call can be made ([] = ready)."""
        return self.settings.aras_missing(self.service)

    def __bool__(self) -> bool:
        return not self.missing

    def describe(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "service": self.service,
            "endpoint": self.client.base_url,
            "model": self.model,
            "ready": bool(self),
            "missing": self.missing,
        }

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = "auto",
    ) -> ModelResponse:
        """One chat completion. Raises :class:`ArasError`, never returns a guess."""
        missing = self.missing
        if missing:
            raise ArasNotConfigured(self.service, missing)

        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        started = time.perf_counter()
        data = await self.client.chat(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ArasError(
                self.service,
                "the router answered without a completion: "
                + json.dumps(data, ensure_ascii=False, default=str)[:300],
                hint="the request reached the router, but its reply has no `choices` — "
                "the body quoted above is everything it said",
            )
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            message = {}

        content = message.get("content")
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        raw_calls = message.get("tool_calls")
        usage = data.get("usage")
        return ModelResponse(
            content=str(content or "").strip(),
            tool_calls=[
                ToolCall.from_openai(call)
                for call in (raw_calls if isinstance(raw_calls, list) else [])
                if isinstance(call, dict)
            ],
            model=str(data.get("model") or self.model),
            provider=PROVIDER,
            usage=usage if isinstance(usage, dict) else {},
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
        )

    async def list_models(self) -> list[str]:
        """The ids the router lists (needs URL + token, not ``ARAS_MODEL``)."""
        return await self.client.models()

    async def ping(self) -> dict[str, Any]:
        """Cheap end-to-end check: do URL + token + model actually answer? Never raises."""
        started = time.perf_counter()
        try:
            response = await self.complete(
                [{"role": "user", "content": "Reply with exactly: pong"}],
                temperature=0,
                max_tokens=16,
            )
        except ArasError as exc:
            return {
                "ok": False,
                "provider": PROVIDER,
                "model": self.model,
                "error": exc.to_dict(),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        return {
            "ok": True,
            "provider": PROVIDER,
            "model": response.model,
            "reply": response.content[:200],
            "latency_ms": response.latency_ms,
        }


class ArasSearch:
    """The orchestrator's ``SearchChain`` port: the ARAS web service, and only it.

    One backend, so there is nothing to fall through to: a failed search is an
    :class:`ArasError` the caller sees, not a silent switch to something else.
    """

    name = PROVIDER
    service = "web"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = ArasWebClient(settings, transport=transport)

    @property
    def missing(self) -> list[str]:
        """Env vars still needed before a search can be made ([] = ready)."""
        return self.settings.aras_missing(self.service)

    def __bool__(self) -> bool:
        return not self.missing

    def describe(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "service": self.service,
            "endpoint": self.client.base_url,
            "ready": bool(self),
            "missing": self.missing,
        }

    async def search(self, query: str, limit: int | None = None) -> SearchResponse:
        """Run one query. ``limit`` is clamped to 1–10 results."""
        missing = self.missing
        if missing:
            raise ArasNotConfigured(self.service, missing)
        try:
            wanted = int(limit or self.settings.agent_max_search_results)
        except (TypeError, ValueError):
            wanted = self.settings.agent_max_search_results
        wanted = max(1, min(wanted, 10))

        started = time.perf_counter()
        hits = await self.client.search(query, max_results=wanted)
        results = [
            SearchResult(
                title=hit["title"],
                url=hit["url"],
                snippet=hit["snippet"],
                source=f"{PROVIDER}:{hit['engine']}" if hit.get("engine") else PROVIDER,
            )
            for hit in hits
            if hit.get("url")
        ]
        return SearchResponse(
            query=query,
            results=results[:wanted],
            provider=PROVIDER,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def ping(self, query: str = "python asyncio tutorial") -> dict[str, Any]:
        """One real query, timed — the /diagnose row. Never raises."""
        started = time.perf_counter()
        try:
            response = await self.search(query, 3)
        except ArasError as exc:
            return {
                "provider": PROVIDER,
                "ok": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": exc.to_dict(),
            }
        report: dict[str, Any] = {
            "provider": PROVIDER,
            "ok": bool(response.results),
            "count": len(response.results),
            "latency_ms": response.latency_ms,
            "sample": [result.to_dict() for result in response.results[:2]],
            "empty": not response.results,
        }
        if not response.results:
            report["error"] = ArasError(
                self.service,
                f"the web service answered, but with no results for '{query}'",
                hint="the service is reachable; the engines behind it returned nothing",
            ).to_dict()
        return report


class ArasKnowledge:
    """The orchestrator's ``KnowledgeStore`` port, backed by retrieval :8105.

    The document agent calls :meth:`retrieve` when a request has no attachment:
    the collection named in ``ARAS_COLLECTION`` is its second source of truth.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.retrieval = ArasRetrievalClient(settings, transport=transport)
        self.collection = settings.aras_collection.strip()
        self.mode = (settings.aras_retrieval_mode or "hybrid").strip().lower()

    def __bool__(self) -> bool:
        return bool(self.retrieval.configured and self.collection)

    def describe(self) -> dict[str, Any]:
        return {"kind": "aras.retrieval", "collection": self.collection, "mode": self.mode}

    async def retrieve(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.retrieval.search(
            query, collection=self.collection, mode=self.mode, limit=limit
        )


class ArasDataBackend:
    """The orchestrator's ``DataBackend`` port, backed by data :8107."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = ArasDataClient(settings, transport=transport)
        self.data_source_id = (settings.aras_data_source_id or "").strip()

    def __bool__(self) -> bool:
        return bool(self.client.configured and self.data_source_id
                    and self.settings.aras_tenant and self.settings.aras_owner)

    def describe(self) -> dict[str, Any]:
        return {"kind": "aras.data", "data_source_id": self.data_source_id}

    async def query(self, question: str) -> dict[str, Any]:
        return await self.client.query(question, data_source_id=self.data_source_id)


# ------------------------------------------------------------------ factory
def build_model(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> ArasModel:
    """The ModelChain port. Always an object — falsy until the router is configured."""
    return ArasModel(settings, transport=transport)


def build_search(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> ArasSearch:
    """The SearchChain port. Always an object — falsy until the web service is configured."""
    return ArasSearch(settings, transport=transport)


def build_knowledge(settings: Settings) -> ArasKnowledge | None:
    """The KnowledgeStore port, or None when RAG is not wired up."""
    knowledge = ArasKnowledge(settings)
    return knowledge if knowledge else None


def build_data_backend(settings: Settings) -> ArasDataBackend | None:
    """The DataBackend port, or None when the data service is not wired up."""
    backend = ArasDataBackend(settings)
    return backend if backend else None


def pdf_reader(settings: Settings):
    """A ``files.extract``-compatible PDF reader backed by OCR :8102, or None.

    ``extract`` is synchronous, so this reader uses a blocking HTTP call; it is
    only ever invoked from the upload route, never inside a run.
    """
    if not settings.aras_configured("ocr"):
        return None
    endpoint = settings.aras_endpoint("ocr")
    token = settings.aras_ocr_token
    timeout = settings.aras_timeout

    def read(name: str, data: bytes) -> tuple[str, int | None]:
        with httpx.Client(timeout=timeout, trust_env=trust_env_for(endpoint)) as client:
            response = client.post(
                f"{endpoint}/api/v1/ocr/",
                files={"file": (name, data, "application/pdf")},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise ArasError("ocr", f"HTTP {response.status_code}: {response.text[:200]}",
                            status=response.status_code)
        payload = response.json()
        return str(payload.get("text") or ""), payload.get("page_count")

    read.__name__ = "aras_ocr_reader"
    return read


# ---------------------------------------------------------------- catalogue
#: What each service is *for* in this app — the text behind GET /providers.
SERVICES: dict[str, dict[str, Any]] = {
    "router": {
        "label": "Router",
        "role": "The model: every planner, agent, evaluator and manager call.",
        "route": "POST /api/v1/chat/completions/",
        "env": ["ARAS_ROUTER_TOKEN", "ARAS_MODEL"],
        "required": True,
    },
    "web": {
        "label": "Web",
        "role": "Web search: the web_search tool of the search agent.",
        "route": "POST /v1/search/",
        "env": ["ARAS_WEB_TOKEN"],
    },
    "ocr": {
        "label": "OCR",
        "role": "Reads uploaded PDFs into text for the document agent.",
        "route": "POST /api/v1/ocr/",
        "env": ["ARAS_OCR_TOKEN"],
    },
    "retrieval": {
        "label": "Retrieval",
        "role": "The document agent's knowledge store (RAG) when no file is attached.",
        "route": "POST /v1/search/",
        "env": ["ARAS_RETRIEVAL_TOKEN", "ARAS_COLLECTION", "ARAS_RETRIEVAL_MODE"],
    },
    "ingestion": {
        "label": "Ingestion",
        "role": "Indexes accepted uploads into the collection when ARAS_AUTO_INGEST=true.",
        "route": "POST /v1/ingest/",
        "env": ["ARAS_INGESTION_TOKEN", "ARAS_COLLECTION", "ARAS_AUTO_INGEST"],
    },
    "data": {
        "label": "Data",
        "role": "The data agent's analytics backend when there is nothing local to compute.",
        "route": "POST /v1/query/",
        "env": ["ARAS_DATA_TOKEN", "ARAS_TENANT", "ARAS_OWNER", "ARAS_DATA_SOURCE_ID"],
    },
    "vector": {
        "label": "Vector store",
        "role": "Health and diagnostics only — ingestion and retrieval are the working doors.",
        "route": "GET /v1/collections/",
        "env": ["ARAS_VECTOR_TOKEN"],
    },
}


def catalogue(settings: Settings) -> list[dict[str, Any]]:
    """Every service: its role here, where it lives and what it still needs.

    ``state`` is the one word a person needs: ``ready``, ``off`` (configured
    but switched off by choice), ``incomplete`` (URL + token are there, another
    value is not) or ``not configured`` (no URL or no token). No secrets.
    """
    out: list[dict[str, Any]] = []
    for name, info in SERVICES.items():
        missing = settings.aras_missing(name)
        if not settings.aras_configured(name):
            state = "not configured"
        elif missing:
            state = "incomplete"
        elif name == "ingestion" and not settings.aras_auto_ingest:
            state = "off"
        else:
            state = "ready"
        out.append(
            {
                "service": name,
                "label": info["label"],
                "port": settings.ARAS_PORTS[name][0],
                "role": info["role"],
                "route": info["route"],
                "endpoint": settings.aras_endpoint(name),
                "state": state,
                "ready": state == "ready",
                "configured": settings.aras_configured(name),
                "required": bool(info.get("required")),
                "env": list(info["env"]),
                "missing_env": missing,
            }
        )
    return out


async def status(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Health of every configured ARAS service — the /aras and /diagnose block."""
    configured = settings.aras_services_configured
    clients = [CLIENTS[name](settings, transport=transport) for name in configured]
    checked = list(await asyncio.gather(*(client.health() for client in clients)))
    missing = {name: settings.aras_missing(name) for name in CLIENTS}
    return {
        "configured": configured,
        "skipped": [name for name in CLIENTS if name not in configured],
        "checked": checked,
        "model": settings.aras_model_id,
        "model_ready": settings.aras_model_ready,
        "search_ready": settings.aras_search_ready,
        "rag_ready": settings.aras_rag_ready,
        "data_ready": settings.aras_data_ready,
        "tenant_set": bool(settings.aras_tenant and settings.aras_owner),
        "missing": {name: envs for name, envs in missing.items() if envs},
    }
