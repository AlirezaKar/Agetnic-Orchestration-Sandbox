"""Clients for the ARAS microservices this orchestrator is being built for.

The other app runs one service per capability (see README §"ARAS
microservices"); this module is the only place that knows their URLs, auth and
JSON shapes. Everything else sees them through the existing seams:

* router :8101  — an OpenAI-compatible LLM provider (``app/llm/presets.py``,
  preset ``aras``) — not in this module at all
* web    :8108  — a search backend (``app/search/providers.py`` imports
  :class:`ArasWebClient` here)
* ocr    :8102  — a PDF reader injected into ``files.extract``
* retrieval :8105 + ingestion :8104 — :class:`ArasKnowledge`, the orchestrator's
  ``KnowledgeStore`` port (RAG for the document agent)
* data   :8107  — :class:`ArasDataBackend`, the orchestrator's ``DataBackend``
  port (natural-language analytics for the data agent)
* vector :8103  — health/diagnostics only; the README says ingestion and
  retrieval are the right doors for normal orchestrator work

Auth is ``Authorization: Bearer <token>`` per service, each token falling back
to the shared ``ARAS_TOKEN`` (``app/config.py``). The data service additionally
wants ``X-Tenant`` and ``X-Owner-External-Id`` on every call.

No secret ever leaves this process: errors carry status + the service's
``error``/``detail`` text, never the token.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import Settings, trust_env_for

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


class ArasError(RuntimeError):
    """One ARAS call failed (network, auth, 4xx/5xx, bad JSON)."""

    def __init__(self, service: str, message: str, *, status: int | None = None, hint: str = ""):
        self.service = service
        self.status = status
        self.hint = hint
        super().__init__(f"[aras:{service}] {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "status": self.status,
            "message": str(self),
            "hint": self.hint,
        }


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
        if not self.configured:
            raise ArasError(
                self.service,
                "not configured",
                hint=f"set ARAS_BASE_URL (or ARAS_{self.service.upper()}_URL) and "
                f"ARAS_{self.service.upper()}_TOKEN (or the shared ARAS_TOKEN) in .env",
            )

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
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error") or body.get("detail") or "")[:300]
            except ValueError:
                detail = response.text[:300]
            raise ArasError(
                self.service,
                f"HTTP {response.status_code}: {detail or 'request failed'}",
                status=response.status_code,
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
    """Web :8108 — public web search + fetch, used by the search chain."""

    service = "web"

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


CLIENTS: dict[str, type[ArasClient]] = {
    "ocr": ArasOcrClient,
    "ingestion": ArasIngestionClient,
    "retrieval": ArasRetrievalClient,
    "vector": ArasVectorClient,
    "web": ArasWebClient,
    "data": ArasDataClient,
}


# ------------------------------------------------------------- port adapters
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


async def status(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Health of every configured ARAS service — the /diagnose block."""
    import asyncio

    configured = settings.aras_services_configured
    checked: list[dict[str, Any]] = []
    if "router" in configured:
        # the router is pinged through the model chain, but its health URL is
        # still the cheapest "is the tunnel up" probe there is
        client = ArasClient(settings, transport=transport)
        client.service = "router"
        client.base_url = settings.aras_endpoint("router")
        client.token = settings.aras_router_token
        checked.append(await client.health())
    others = [CLIENTS[name](settings, transport=transport)
              for name in configured if name != "router"]
    checked.extend(await asyncio.gather(*(client.health() for client in others)))
    return {
        "configured": configured,
        "skipped": [name for name in ("router", *CLIENTS) if name not in configured],
        "checked": checked,
        "rag_ready": settings.aras_rag_ready,
        "data_ready": settings.aras_data_ready,
        "tenant_set": bool(settings.aras_tenant and settings.aras_owner),
    }
