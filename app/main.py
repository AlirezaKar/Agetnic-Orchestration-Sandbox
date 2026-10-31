"""HTTP surface: test-harness UI, diagnostics, direct search, and the agent loop.

Every API route is served **twice** — with and without the ``/api`` prefix —
so a reverse proxy, a rewrite rule or a frontend that assumes ``/api/chat``
still reaches the backend instead of returning ``{"detail":"Not Found"}``.

The UI is served with relative asset URLs and ``Cache-Control: no-store``:
the page therefore works when it is mounted at a sub-path (``/ui/``,
``/sandbox/``, a proxy prefix), and a browser can never keep an old HTML that
points at assets from a previous version.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import logs
from app.agent import WebSearchAgent
from app.config import get_settings
from app.llm.base import ProviderError
from app.llm.registry import (
    ConfigurationError,
    build_chain,
    build_provider,
    describe_providers,
    resolve_named,
)
from app.search.base import SearchError
from app.search.presets import SEARCH_PRESETS
from app.search.providers import PROVIDER_CLASSES
from app.search.registry import build_search_chain, describe_search_providers
from app.version import VERSION

STATIC_DIR = Path(__file__).parent / "static"
ASSETS_DIR = STATIC_DIR / "assets"
INDEX_FILE = STATIC_DIR / "index.html"

API_ROUTES = [
    "GET  /version",
    "GET  /health",
    "GET  /config",
    "GET  /providers",
    "GET  /providers/{name}/models",
    "POST /search",
    "GET  /search/diagnose",
    "POST /chat",
    "GET  /diagnose",
    "GET  /                           (test harness UI)",
    "GET  /assets/…                   (css + js)",
]

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start logging for plain `uvicorn app.main:app` too (run.py configures first)."""
    summary = logs.setup_logging()
    logs.install_exception_hooks()
    logs.enable_faulthandler()
    if not logs.startup_recorded():  # run.py already recorded this start
        logs.record_startup(VERSION, start="uvicorn app.main:app", port=get_settings().port)
    logs.get_logger("api").info(
        "ready version=%s logs=%s writable=%s routes=%d",
        VERSION,
        summary["dir"],
        summary["writable"],
        len(API_ROUTES),
    )
    yield
    logs.get_logger("api").info("shutdown version=%s", VERSION)


app = FastAPI(
    title="Agetnic Orchestration Sandbox",
    version=VERSION,
    description="Pluggable free model providers + web search, no mock model.",
    lifespan=lifespan,
)

# every request: one line in logs/requests.log, timings, x-request-id
app.add_middleware(logs.RequestLoggingMiddleware)

router = APIRouter()


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
    if isinstance(exc, ProviderError):
        body["detail"] = exc.to_dict()
    elif isinstance(exc, ConfigurationError):
        body["detail"] = {"missing": exc.missing}
    elif isinstance(exc, SearchError):
        body["detail"] = exc.to_dict()
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


# ------------------------------------------------------------------ UI
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_FILE, headers={"Cache-Control": "no-store, must-revalidate"})


@app.head("/", include_in_schema=False)
async def index_head() -> Response:
    # health checks / proxies sometimes probe with HEAD; answer it instead of 405
    return Response(
        status_code=200,
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/ui", include_in_schema=False)
async def ui_redirect() -> RedirectResponse:
    # relative redirect: keeps working when the app is mounted under a prefix
    return RedirectResponse(url=".", status_code=307)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> JSONResponse:
    return JSONResponse(status_code=204, content=None)


if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


# ---------------------------------------------------------------- routes
@router.head("/health", include_in_schema=False)
async def health_head() -> Response:
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.get("/version")
async def version(request: Request) -> dict[str, Any]:
    """One tiny call that answers "is the page I am looking at current?"."""
    return {
        "version": VERSION,
        "ui_url": str(request.base_url).rstrip("/") + "/",
        "api_prefixes": ["", "/api"],
        "changelog": f"logs/changelog/v{VERSION}.md",
        "logs": str(logs.log_dir()),
    }


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings = get_settings()
    chain = build_chain(settings)
    search = build_search_chain(settings)
    return {
        "status": "ok",
        "version": VERSION,
        "api_prefixes": ["", "/api"],
        "model_provider_ready": bool(chain),
        "model_chain": [p.name for p in chain.providers],
        "search_chain": [p.name for p in search.providers],
        "skipped": {"models": chain.skipped, "search": search.skipped},
        "env_file": Path(".env").exists(),
        "ui": {
            "index": INDEX_FILE.exists(),
            "assets": ASSETS_DIR.exists(),
            "mount": str(request.base_url).rstrip("/"),
        },
    }


@router.get("/config")
async def config() -> dict[str, Any]:
    settings = get_settings()
    return {**settings.public_summary(), "version": VERSION, "env_file_present": Path(".env").exists()}


@router.get("/providers")
async def providers() -> dict[str, Any]:
    settings = get_settings()
    chain = build_chain(settings)
    search = build_search_chain(settings)
    return {
        "core": {
            "provider_env": "LLM_PROVIDER",
            "value": settings.llm_provider,
            "provider_mode": "auto" if settings.is_auto_provider else "manual",
            "model_override_env": "LLM_MODEL",  # manual mode only
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


@router.get("/providers/{name}/models")
async def provider_models(name: str) -> Any:
    settings = get_settings()
    try:
        resolved = resolve_named(name.lower(), settings)
        provider = build_provider(resolved, settings)
        models = await provider.list_models()
        try:
            recommended = (await provider.ranked_models())[:10]
        except ProviderError:  # pragma: no cover - listing already succeeded
            recommended = []
    except ConfigurationError as exc:
        logs.get_logger("api").warning("provider %s not configured: %s", name, exc)
        return _error(400, "configuration", exc)
    except ProviderError as exc:
        logs.get_logger("api").warning("provider %s failed: %s", name, exc)
        return _error(502, "provider", exc)
    return {
        "provider": name.lower(),
        "endpoint": provider.base_url,
        "count": len(models),
        "recommended": recommended,
        "configured_model": provider.model,
        "models": models,
        "models_url": resolved.preset.models_url,
    }


@router.post("/search")
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
        logs.get_logger("search").warning("search failed [%s]: %s", exc.provider, exc)
        return _error(502, "search", exc)
    return response.to_dict()


@router.get("/search/diagnose")
async def search_diagnose(
    q: str = Query(default="python asyncio tutorial"),
    all_providers: bool = Query(default=False, alias="all"),
) -> dict[str, Any]:
    """Run every usable search backend once and report pass/fail + latency."""
    settings = get_settings()
    targets: list[str] = []
    skipped: list[dict[str, str]] = []
    for name in PROVIDER_CLASSES if all_providers else settings.search_chain:
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


@router.post("/chat")
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
        logs.get_logger("chat").warning("chat rejected, no provider: %s", exc)
        return _error(400, "configuration", exc)
    except ProviderError as exc:
        logs.get_logger("chat").warning("chat failed [%s]: %s", exc.provider, exc)
        return _error(502, "provider", exc)
    return result.to_dict()


@router.get("/diagnose")
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
            resolved = resolve_named(provider.lower(), settings)
            ping_target = build_provider(resolved, settings)
        elif chain:
            ping_target = chain.providers[0]
        else:
            ping_target = None
        if ping_target is not None:
            model_report = await ping_target.ping()
    except (ConfigurationError, ProviderError) as exc:
        model_report = {
            "ok": False,
            "error": getattr(exc, "to_dict", lambda: {"message": str(exc)})(),
        }

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
        "version": VERSION,
        "model": model_report,
        "search": {
            "checked": search_out,
            "skipped": search_chain.skipped,
            "ok": [r["provider"] for r in search_out if r.get("ok")],
            "failed": [r["provider"] for r in search_out if not r.get("ok")],
        },
        "hints": _hints(settings, model_report, search_out),
    }


def _hints(settings, model: dict[str, Any], search: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    if not model.get("ok"):
        hints.append(
            "Model not answering: set LLM_PROVIDER and the matching key in .env "
            "(GET /providers lists every free option with links), then retry."
        )
    if not search:
        hints.append("No search backend configured: SEARCH_PROVIDER=ddg needs no key at all.")
    if search and all(not r.get("ok") for r in search):
        hints.append(
            "Every search backend failed. On a VPS DuckDuckGo often bot-challenges; "
            "try SEARCH_PROVIDERS=tavily,ddg or SEARCH_PROVIDER=wikipedia."
        )
    if not Path(".env").exists():
        hints.append("No .env file found; copy .env.example to .env first.")
    return hints


# ------------------------------------------------- alias: same API on /api
# The UI negotiates its own base URL, but proxies, rewrites and older clients
# often call /api/chat. Serving both keeps them working instead of 404ing.
app.include_router(router)
app.include_router(router, prefix="/api", include_in_schema=False)


@app.get("/api", include_in_schema=False)
@app.get("/api/", include_in_schema=False)
async def api_root(request: Request) -> dict[str, Any]:
    return {
        "service": "Agetnic Orchestration Sandbox",
        "version": VERSION,
        "ui": str(request.base_url).rstrip("/"),
        "routes": API_ROUTES,
    }


# ---------------------------------------------------------- crash capture
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Never let a traceback disappear into the terminal — it goes to error.log."""
    request_id = request.headers.get("x-request-id") or "-"
    logs.get_logger("crash").exception(
        "unhandled error on %s %s req=%s", request.method, request.url.path, request_id
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal",
            "message": f"{type(exc).__name__}: {exc}",
            "request_id": request_id,
            "hint": "The full traceback is in logs/error.log (python -m scripts.logs errors).",
        },
        headers={"x-request-id": request_id},
    )


# ------------------------------------------------------- helpful 404s
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    if request.method == "HEAD":
        # uvicorn rejects a body on HEAD; answer with headers only
        return Response(
            status_code=exc.status_code,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    if exc.status_code != 404:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    path = request.url.path
    hint = (
        "The UI is served by this FastAPI app, not by a static file server. "
        "Open the app root (http://localhost:8000/) instead of the .html file, "
        "and start it with `python run.py` or "
        "`uvicorn app.main:app --host 0.0.0.0 --port 8000`."
    )
    if path.startswith("/api"):
        hint = (
            "Every API route is served both with and without the /api prefix "
            "(e.g. POST /chat and POST /api/chat). This path matches neither — "
            "check the route name and that your proxy forwards the full path. "
            + hint
        )
    if path.startswith("/assets"):
        hint = (
            "Static assets are mounted at /assets from app/static/assets. "
            "If the repository was copied without that folder, the UI loads unstyled. "
            + hint
        )
    return JSONResponse(
        status_code=404,
        content={
            "error": "not_found",
            "message": f"No route for {request.method} {path}",
            "hint": hint,
            "routes": API_ROUTES,
            "openapi": str(request.base_url).rstrip("/") + "/docs",
        },
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.reload)
