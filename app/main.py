"""HTTP surface: playground, diagnostics, direct search, and the agent loop."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agent import WebSearchAgent
from app.config import Settings, get_settings
from app.llm.base import ProviderError
from app.llm.registry import (
    ConfigurationError,
    build_chain,
    build_provider,
    describe_providers,
    resolve_provider,
)
from app.search.base import SearchError
from app.search.presets import SEARCH_PRESETS
from app.search.providers import PROVIDER_CLASSES
from app.search.registry import build_search_chain, describe_search_providers

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Agetnic Orchestration Sandbox",
    version="0.2.0",
    description="Pluggable free model providers + web search, no mock model.",
)


# --------------------------------------------------------------- schemas
class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict[str, Any]] = []
    max_steps: int | None = Field(default=None, ge=1, le=10)
    allow_search: bool = True


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=5, ge=1, le=10)
    provider: str | None = None


# ---------------------------------------------------------------- helpers
def _error(status: int, kind: str, exc: Exception, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": kind, "message": str(exc)}
    if isinstance(exc, (ProviderError, ConfigurationError)):
        body["detail"] = exc.to_dict() if isinstance(exc, ProviderError) else {"missing": exc.missing}
    if isinstance(exc, SearchError):
        body["detail"] = exc.to_dict()
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


# ----------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    chain = build_chain(settings)
    search = build_search_chain(settings)
    return {
        "status": "ok",
        "version": app.version,
        "model_provider_ready": bool(chain),
        "model_chain": [p.name for p in chain.providers],
        "search_chain": [p.name for p in search.providers],
        "skipped": {"models": chain.skipped, "search": search.skipped},
        "env_file": os.path.exists(".env"),
    }


@app.get("/config")
async def config() -> dict[str, Any]:
    settings = get_settings()
    return {**settings.public_summary(), "env_file_present": os.path.exists(".env")}


@app.get("/providers")
async def providers() -> dict[str, Any]:
    settings = get_settings()
    chain = build_chain(settings)
    search = build_search_chain(settings)
    return {
        "core": {
            "provider_env": "LLM_PROVIDER",
            "value": settings.llm_provider,
            "model_override_env": "LLM_MODEL",
            "model_override": settings.llm_model,
            "chain": settings.llm_chain,
            "ready": [p.name for p in chain.providers],
            "skipped": chain.skipped,
        },
        "providers": describe_providers(settings),
        "search": {
            "chain": settings.search_chain,
            "ready": [p.name for p in search.providers],
            "skipped": search.skipped,
            "providers": describe_search_providers(settings),
        },
    }


@app.get("/providers/{name}/models")
async def provider_models(name: str) -> Any:
    settings = get_settings()
    try:
        resolved = resolve_provider(name.lower(), settings)
        provider = build_provider(resolved, settings)
        models = await provider.list_models()
    except ConfigurationError as exc:
        return _error(400, "configuration", exc)
    except ProviderError as exc:
        return _error(502, "provider", exc)
    return {
        "provider": name.lower(),
        "endpoint": provider.base_url,
        "count": len(models),
        "models": models,
        "models_url": resolved.preset.models_url,
    }


@app.post("/search")
async def search(request: SearchRequest) -> Any:
    settings = get_settings()
    try:
        if request.provider:
            key = request.provider.lower()
            if key not in PROVIDER_CLASSES:
                raise SearchError(key, f"unknown provider (known: {', '.join(PROVIDER_CLASSES)})")
            provider = PROVIDER_CLASSES[key](settings)
            results = await provider.search(request.query, request.max_results)
            return {
                "query": request.query,
                "provider": key,
                "count": len(results),
                "results": [r.to_dict() for r in results],
            }
        chain = build_search_chain(settings)
        response = await chain.search(request.query, request.max_results)
    except SearchError as exc:
        return _error(502, "search", exc)
    return response.to_dict()


@app.get("/search/diagnose")
async def search_diagnose(
    q: str = Query(default="python asyncio tutorial"),
    all_providers: bool = Query(default=False, alias="all"),
) -> dict[str, Any]:
    """Run every usable search backend once and report pass/fail + latency."""
    settings = get_settings()
    targets: list[str] = []
    skipped: list[dict[str, str]] = []
    for name in (PROVIDER_CLASSES if all_providers else settings.search_chain):
        if name not in PROVIDER_CLASSES:
            skipped.append({"provider": name, "reason": "unknown provider"})
            continue
        preset = SEARCH_PRESETS[name]
        missing = [env for env in preset.key_env if not getattr(settings, env.lower(), None)]
        if missing:
            skipped.append({"provider": name, "reason": f"missing {', '.join(missing)}"})
            continue
        targets.append(name)

    reports = await asyncio.gather(
        *(PROVIDER_CLASSES[name](settings).ping(q) for name in targets),
        return_exceptions=True,
    )
    out = []
    for name, report in zip(targets, reports):
        if isinstance(report, BaseException):
            out.append({"provider": name, "ok": False, "error": {"message": str(report)}})
        else:
            out.append(report)
    return {
        "query": q,
        "checked": out,
        "skipped": skipped,
        "summary": {
            "ok": [r["provider"] for r in out if r.get("ok")],
            "failed": [r["provider"] for r in out if not r.get("ok")],
        },
    }


@app.post("/chat")
async def chat(request: ChatRequest) -> Any:
    settings = get_settings()
    chain = build_chain(settings)
    search_chain = build_search_chain(settings)
    agent = WebSearchAgent(chain, search_chain, settings)
    try:
        result = await agent.run(
            request.message,
            history=request.history,
            max_steps=request.max_steps,
            allow_search=request.allow_search,
        )
    except ConfigurationError as exc:
        return _error(400, "configuration", exc)
    except ProviderError as exc:
        return _error(502, "provider", exc)
    return result.to_dict()


@app.get("/diagnose")
async def diagnose(
    provider: str | None = None,
    q: str = Query(default="python asyncio tutorial"),
) -> dict[str, Any]:
    """One-stop verification: model ping + search pings + config notes."""
    settings = get_settings()
    chain = build_chain(settings)

    model_report: dict[str, Any] = {"ok": False, "error": {"message": "no provider configured"}}
    try:
        if provider:
            resolved = resolve_provider(provider.lower(), settings)
            ping_target = build_provider(resolved, settings)
        elif chain:
            ping_target = chain.providers[0]
        else:
            ping_target = None
        if ping_target is not None:
            model_report = await ping_target.ping()
    except (ConfigurationError, ProviderError) as exc:
        model_report = {"ok": False, "error": getattr(exc, "to_dict", lambda: {"message": str(exc)})()}

    search_chain = build_search_chain(settings)
    search_reports = await asyncio.gather(
        *(p.ping(q) for p in search_chain.providers), return_exceptions=True
    )
    search_out = []
    for p, report in zip(search_chain.providers, search_reports):
        if isinstance(report, BaseException):
            search_out.append({"provider": p.name, "ok": False, "error": {"message": str(report)}})
        else:
            search_out.append(report)

    return {
        "model": model_report,
        "search": {
            "checked": search_out,
            "skipped": search_chain.skipped,
            "ok": [r["provider"] for r in search_out if r.get("ok")],
            "failed": [r["provider"] for r in search_out if not r.get("ok")],
        },
        "hints": _hints(settings, model_report, search_out),
    }


def _hints(settings: Settings, model: dict[str, Any], search: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    if not model.get("ok"):
        hints.append(
            "Model not answering: set LLM_PROVIDER and the matching key in .env "
            "(GET /providers lists every free option with links), then retry."
        )
    if not search:
        hints.append(
            "No search backend configured: SEARCH_PROVIDER=ddg needs no key at all."
        )
    if search and all(not r.get("ok") for r in search):
        hints.append(
            "Every search backend failed. On a VPS DuckDuckGo often bot-challenges; "
            "try SEARCH_PROVIDERS=tavily,ddg or SEARCH_PROVIDER=wikipedia."
        )
    if not os.path.exists(".env"):
        hints.append("No .env file found; copy .env.example to .env first.")
    return hints


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.reload)
