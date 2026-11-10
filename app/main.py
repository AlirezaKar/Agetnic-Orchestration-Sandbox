"""HTTP surface: the studio UI, the orchestrator, and the ARAS diagnostics.

This module is the host: it serves the page, wires the orchestrator's ports to
the ARAS microservices (``app/aras.py`` — router = the model, web = the search,
retrieval/data/OCR for the agents) and exposes the routes the page calls.
There is no other model and no other search backend to fall back to.

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
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import aras, logs
from app.agent import WebSearchAgent
from app.aras import ArasError, ArasNotConfigured, build_model, build_search
from app.config import get_settings
from app.memory import SessionMemory, session_view
from app.orchestrator import (
    AUDIT_LEVELS,
    BUS,
    audit_catalogue,
    load_audit,
    run_audit,
    write_audit,
    AttachmentStore,
    FileError,
    OrchestrationRequest,
    OrchestratorService,
    decode_payload,
    new_run_id,
    runlog,
    plain_thought,
    thought as live_thought,
)
from app.version import VERSION

STATIC_DIR = Path(__file__).parent / "static"
ASSETS_DIR = STATIC_DIR / "assets"
INDEX_FILE = STATIC_DIR / "index.html"

API_ROUTES = [
    "GET  /version",
    "GET  /health",
    "GET  /config",
    "GET  /providers                  (the ARAS services: role, endpoint, what is missing)",
    "POST /search                     (ARAS web search, no model)",
    "POST /chat                       (one model + the web_search tool)",
    "POST /orchestrate                (manager + parallel agents)",
    "POST /orchestrate/stream         (the same turn, streamed over SSE)",
    "POST /files                      (attachment upload)",
    "GET  /files                      (attachments in the store)",
    "POST /files/{id}/consent         (allow / refuse an attachment)",
    "DELETE /files/{id}               (remove an attachment)",
    "GET  /agents                     (agent catalogue + planner mode)",
    "GET  /audit                      (self-audit: the checks + the last scorecard)",
    "POST /audit/run                  (run the self-audit, offline by default)",
    "GET  /audit/stream               (the same audit, streamed over SSE)",
    "GET  /audit/last                 (the newest scorecard on disk)",
    "GET  /runs                       (orchestration log: recent runs)",
    "GET  /runs/{run_id}              (full trace of one run)",
    "GET  /sessions                   (harness memory: past conversations)",
    "POST /sessions                   (start a new conversation)",
    "GET  /sessions/{id}              (one conversation, replayable turn by turn)",
    "PATCH /sessions/{id}             (rename a conversation)",
    "DELETE /sessions/{id}            (forget a conversation)",
    "GET  /diagnose                   (model ping + search ping + ARAS health)",
    "GET  /aras                       (health of every configured ARAS service)",
    "GET  /aras/models                (model ids the router lists)",
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
    description="A manager-led agent orchestrator running on the ARAS microservices.",
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
    # Harness memory: continue this conversation (created when unknown) and
    # store this exchange. ``remember=false`` keeps it out of memory.
    session_id: str | None = None
    remember: bool = True


class FileUploadRequest(BaseModel):
    name: str = Field(default="pasted.txt", max_length=200)
    text: str | None = None
    data_b64: str | None = None
    # The user's answer to the permission prompt. None = not asked yet: the file
    # is stored but no agent may read it until POST /files/{id}/consent allows it
    # (only when FILES_PERMISSION_PROMPT=true — otherwise it is allowed at once).
    consent: bool | None = None


class FileConsentRequest(BaseModel):
    granted: bool = True


class OrchestrateRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[dict[str, Any]] = []
    file_ids: list[str] = []
    # Optional per-request overrides of the .env defaults.
    agents: list[str] | None = None
    planner: str | None = Field(default=None, pattern="^(auto|heuristic|llm)$")
    synthesize: bool | None = None
    review: bool | None = None
    evaluate: bool | None = None
    # Harness memory: continue this conversation (created when unknown) and
    # store this exchange, both sides, exactly as the page rendered it.
    session_id: str | None = None
    remember: bool = True


class AuditRequest(BaseModel):
    """A self-audit run: offline by default, live only when asked for."""

    live: bool = False
    #: Restrict to these check ids (empty = every check).
    only: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=5, ge=1, le=10)
    session_id: str | None = None
    remember: bool = True


class SessionCreateRequest(BaseModel):
    title: str = Field(default="", max_length=120)
    mode: str = Field(default="auto", pattern="^(auto|agent|answer|search)$")


class SessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


# ------------------------------------------------------------ attachments
_STORE: AttachmentStore | None = None


def get_store() -> AttachmentStore:
    """One attachment store per process, rooted at settings.files_dir."""
    global _STORE
    if _STORE is None:
        _STORE = AttachmentStore(get_settings().files_dir)
    return _STORE


def reset_store() -> None:
    """Tests (and a changed FILES_DIR) get a fresh store."""
    global _STORE
    _STORE = None


# ---------------------------------------------------------- harness memory
_MEMORY: SessionMemory | None = None


def get_memory() -> SessionMemory:
    """One conversation store per process, rooted at settings.memory_dir."""
    global _MEMORY
    if _MEMORY is None:
        settings = get_settings()
        _MEMORY = SessionMemory(
            settings.memory_dir,
            enabled=settings.harness_memory,
            max_turns=settings.memory_max_turns,
            history_turns=settings.memory_history_turns,
            history_chars=settings.memory_history_chars,
        )
    return _MEMORY


def reset_memory() -> None:
    """Tests (and a changed MEMORY_DIR) get a fresh conversation store."""
    global _MEMORY
    _MEMORY = None


def _history_for(payload, sessions: SessionMemory) -> list[dict[str, Any]]:
    """Earlier turns for the model: the stored conversation wins over the UI's replay.

    A browser that was reloaded no longer has to resend the chat — the harness
    remembers it. ``history`` in the request stays as the fallback for a session
    the store has never seen (or for memory switched off).
    """
    session_id = getattr(payload, "session_id", None)
    stored = sessions.history(session_id) if session_id else []
    return stored or list(getattr(payload, "history", []) or [])


def _remember(
    sessions: SessionMemory,
    payload,
    *,
    mode: str,
    user_text: str,
    files: list[dict[str, Any]],
    response: dict[str, Any] | None = None,
    ok: bool = True,
    error: dict[str, Any] | None = None,
    run_id: str = "",
    provider: str = "",
    model: str = "",
    latency_ms: int = 0,
) -> dict[str, Any] | None:
    """Store one exchange in the harness memory (no-op when disabled)."""
    if not getattr(payload, "remember", True):
        return None
    return sessions.append(
        getattr(payload, "session_id", None),
        mode=mode,
        user_text=user_text,
        user_files=files,
        response=response,
        ok=ok,
        error=error,
        run_id=run_id,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
    )


def _file_refs(attachments) -> list[dict[str, Any]]:
    return [
        {"id": item.id, "name": item.name, "kind": item.kind, "chars": item.chars}
        for item in attachments
    ]


# ---------------------------------------------------------------- helpers
def _file_error(exc: FileError) -> JSONResponse:
    body: dict[str, Any] = {"error": "file", "message": str(exc)}
    if exc.hint:
        body["hint"] = exc.hint
    return JSONResponse(status_code=exc.status, content=body)


def _error_body(kind: str, exc: Exception) -> dict[str, Any]:
    body: dict[str, Any] = {"error": kind, "message": str(exc)}
    if isinstance(exc, ArasError):
        # service, HTTP status, hint — and the missing env vars when that is why
        body["detail"] = exc.to_dict()
    return body


def _aras_failure(exc: ArasError) -> tuple[int, str]:
    """``(status, kind)`` for an ARAS failure: ours to fix (400) or theirs (502)."""
    if isinstance(exc, ArasNotConfigured):
        return 400, "configuration"
    return 502, "aras"


def _describe_port(port: Any) -> dict[str, Any]:
    """What is wired on a port, for /health: the ARAS adapters describe themselves."""
    describe = getattr(port, "describe", None)
    if callable(describe):
        return dict(describe())
    return {"provider": type(port).__name__, "ready": bool(port), "missing": []}


def _error(status: int, kind: str, exc: Exception, **extra: Any) -> JSONResponse:
    body = _error_body(kind, exc)
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _payload_text(payload: Any) -> str:
    """The user's words in this request (``message`` for chat, ``query`` for search)."""
    return getattr(payload, "message", None) or getattr(payload, "query", "") or ""


def _remembered_error(
    sessions: SessionMemory,
    payload: Any,
    *,
    mode: str,
    status: int,
    kind: str,
    exc: Exception,
    files: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """A failed turn is a turn too: it is remembered, so the transcript stays honest."""
    status, body = _remembered_error_body(
        sessions, payload, mode=mode, status=status, kind=kind, exc=exc, files=files
    )
    return JSONResponse(status_code=status, content=body)


def _remembered_error_body(
    sessions: SessionMemory,
    payload: Any,
    *,
    mode: str,
    status: int,
    kind: str,
    exc: Exception,
    files: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Same contract as :func:`_remembered_error`, as ``(status, body)``."""
    body = _error_body(kind, exc)
    body["session"] = _remember(
        sessions,
        payload,
        mode=mode,
        user_text=_payload_text(payload),
        files=files or [],
        ok=False,
        error={"status": status, "body": dict(body)},
    )
    return status, body


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
async def favicon() -> Response:
    """Answer the browser quietly: a body here used to collide with Content-Length."""
    return Response(status_code=204, media_type=None)


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
        "memory": str(get_memory().dir),
        "sessions": "/sessions",
    }


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings = get_settings()
    model = _describe_port(build_model(settings))
    search = _describe_port(build_search(settings))
    store_files = get_store().list()
    return {
        "status": "ok",
        "version": VERSION,
        "api_prefixes": ["", "/api"],
        # One model (the ARAS router) and one search (ARAS web): each list is
        # ["aras"] when that service is ready and [] when it is not — `model`
        # and `search` below say what is missing.
        "model_provider_ready": bool(model.get("ready")),
        "model_chain": [model["provider"]] if model.get("ready") else [],
        "search_chain": [search["provider"]] if search.get("ready") else [],
        "model": model,
        "search": search,
        "aras": {
            "configured": settings.aras_services_configured,
            "rag_ready": settings.aras_rag_ready,
            "data_ready": settings.aras_data_ready,
        },
        "env_file": Path(".env").exists(),
        "orchestrator": {
            "enabled": settings.orchestrator_enabled,
            "planner": settings.orchestrator_planner,
            "synthesize": settings.orchestrator_synthesize,
            "evaluate": settings.orchestrator_evaluate,
            "optimize": settings.orchestrator_optimize,
            "review": settings.orchestrator_review,
            "review_rounds": settings.orchestrator_review_rounds,
            "agents": settings.orchestrator_roster,
        },
        "files": {
            "count": len(store_files),
            "pending": sum(1 for item in store_files if not item.consent),
            "permission_prompt": settings.files_permission_prompt,
            "writable": get_store().writable,
        },
        "memory": get_memory().stats(),
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
    """The ARAS services behind every port: role, endpoint, state, what is missing.

    Static and free (no network call): ``GET /aras`` is the live health of the
    same list, ``GET /diagnose`` adds a real model call and a real search.
    """
    settings = get_settings()
    return {
        "version": VERSION,
        "model": _describe_port(build_model(settings)),
        "search": _describe_port(build_search(settings)),
        "services": aras.catalogue(settings),
        "env_file": Path(".env").exists(),
    }


@router.post("/search")
async def search(request: SearchRequest) -> Any:
    """One ARAS web search, no model: the quickest test of the web service alone."""
    settings = get_settings()
    sessions = get_memory()
    try:
        response = await build_search(settings).search(request.query, request.max_results)
    except ArasError as exc:
        status, kind = _aras_failure(exc)
        logs.get_logger("search").warning("search failed: %s", exc)
        return _remembered_error(sessions, request, mode="search", status=status, kind=kind, exc=exc)
    payload = response.to_dict()
    payload["session"] = _remember(
        sessions, request, mode="search", user_text=request.query, files=[], response=payload
    )
    return payload


@router.post("/chat")
async def chat(request: ChatRequest) -> Any:
    """One model and one tool (``web_search``), no orchestration.

    ``allow_search=false`` is the plainest possible call: a single completion
    from the ARAS router — the quickest test of the router alone.
    """
    settings = get_settings()
    sessions = get_memory()
    mode = "agent" if request.allow_search else "answer"
    agent = WebSearchAgent(build_model(settings), build_search(settings), settings)
    try:
        result = await agent.run(
            request.message,
            history=_history_for(request, sessions),
            max_steps=request.max_steps,
            allow_search=request.allow_search,
        )
    except ArasError as exc:
        status, kind = _aras_failure(exc)
        logs.get_logger("chat").warning("chat failed: %s", exc)
        return _remembered_error(sessions, request, mode=mode, status=status, kind=kind, exc=exc)
    payload = result.to_dict()
    payload["session"] = _remember(
        sessions, request, mode=mode, user_text=request.message, files=[], response=payload
    )
    return payload


@router.post("/files")
async def upload_file(payload: FileUploadRequest) -> Any:
    """Accept one attachment (base64 or plain text) and return its parsed summary.

    Permission: with ``FILES_PERMISSION_PROMPT=true`` the page asks first and
    sends ``consent: true`` for an allowed file. An upload without that answer
    is stored as *pending* — ``needs_consent: true`` — and ``POST /orchestrate``
    will refuse to hand it to an agent until ``POST /files/{id}/consent`` says
    yes. A workspace that does not ask (the default) allows the file here.
    """
    settings = get_settings()
    try:
        attachment = decode_payload(
            payload.model_dump(),
            max_chars=settings.files_max_chars,
            max_mb=settings.files_max_mb,
            # PDFs go through the ARAS OCR service when it is configured;
            # the local pypdf parse remains the fallback either way.
            pdf_reader=aras.pdf_reader(settings),
        )
    except FileError as exc:
        return _file_error(exc)
    attachment.consent = (
        True if not settings.files_permission_prompt else payload.consent is True
    )
    store = get_store()
    store.save(attachment)
    logs.get_logger("files").info(
        "upload %s kind=%s chars=%s truncated=%s consent=%s",
        attachment.name,
        attachment.kind,
        attachment.chars,
        attachment.truncated,
        attachment.consent,
    )
    body = dict(attachment.summary())
    body["stored"] = store.writable
    # Opt-in: index the accepted upload into the ARAS collection as well, so
    # the knowledge store the document agent retrieves from keeps growing.
    if (
        attachment.consent
        and settings.aras_auto_ingest
        and settings.aras_configured("ingestion")
        and settings.aras_collection.strip()
    ):
        try:
            ingested = await aras.ArasIngestionClient(settings).ingest(
                f"{Path(attachment.name).stem}.txt",
                attachment.text.encode("utf-8"),
                collection=settings.aras_collection.strip(),
            )
            body["aras_ingest"] = ingested
        except aras.ArasError as exc:
            body["aras_ingest"] = {"success": False, "error": str(exc)}
            logs.get_logger("files").warning("aras ingest failed for %s: %s", attachment.name, exc)
    if not store.writable:
        body["warning"] = (
            "the server could not write FILES_DIR, so this file is kept in memory "
            f"for this session only ({store.last_error or 'permission denied'})"
        )
    if not attachment.consent:
        body["needs_consent"] = True
        body["message"] = (
            "stored, waiting for permission: allow it with "
            f"POST /files/{attachment.id}/consent or the prompt on the page"
        )
    return body


@router.post("/files/{file_id}/consent")
async def file_consent(file_id: str, payload: FileConsentRequest) -> Any:
    """The user's answer to the permission prompt: allow the file, or drop it."""
    store = get_store()
    if not payload.granted:
        attachment = store.get(file_id)
        if attachment is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "message": f"no attachment '{file_id}'"},
            )
        store.delete(file_id)
        logs.get_logger("files").info("permission refused for %s — deleted", attachment.name)
        return {
            "id": attachment.id,
            "name": attachment.name,
            "consent": False,
            "deleted": True,
            "message": "the file was not allowed and has been removed",
        }
    attachment = store.set_consent(file_id, True)
    if attachment is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"no attachment '{file_id}'"},
        )
    logs.get_logger("files").info("permission granted for %s", attachment.name)
    body = dict(attachment.summary())
    body["allowed"] = True
    return body


@router.get("/files")
async def list_files() -> dict[str, Any]:
    items = get_store().list()
    return {
        "count": len(items),
        "pending": sum(1 for item in items if not item.consent),
        "permission_prompt": get_settings().files_permission_prompt,
        "files": [item.summary() for item in items],
    }


@router.delete("/files/{file_id}")
async def delete_file(file_id: str) -> Any:
    if not get_store().delete(file_id):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"no attachment '{file_id}'"},
        )
    return {"deleted": file_id}


def _orchestrator(settings) -> OrchestratorService:
    """Wire the orchestrator service with this host's ports (the only place that does)."""
    return OrchestratorService(
        settings,
        models=build_model(settings),  # ARAS router :8101
        search=build_search(settings),  # ARAS web :8108
        agent_loop=WebSearchAgent,
        knowledge=aras.build_knowledge(settings),
        data_backend=aras.build_data_backend(settings),
    )


@router.get("/agents")
async def agents() -> dict[str, Any]:
    """What the orchestrator can do — used by the UI to explain Auto mode."""
    return _orchestrator(get_settings()).describe()


def _orchestration_inputs(payload: OrchestrateRequest) -> Any:
    """Files and consent, or the JSONResponse that ends the request early."""
    settings = get_settings()
    if not settings.orchestrator_enabled:
        return JSONResponse(
            status_code=503,
            content={
                "error": "disabled",
                "message": "The orchestrator is disabled (ORCHESTRATOR_ENABLED=false).",
                "hint": "Use /chat instead, or re-enable it in .env.",
            },
        )

    store = get_store()
    attachments = []
    for file_id in payload.file_ids[:12]:
        attachment = store.get(file_id)
        if attachment is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "file_not_found",
                    "message": f"attachment '{file_id}' is not in the store",
                    "hint": "Upload it again with POST /files; the page does this automatically.",
                },
            )
        attachments.append(attachment)

    waiting = [item for item in attachments if not item.consent]
    if waiting:
        # The user has not allowed these files, so no agent sees a byte of them.
        return JSONResponse(
            status_code=403,
            content={
                "error": "consent_required",
                "message": "permission is still pending for: "
                + ", ".join(item.name for item in waiting),
                "files": _file_refs(waiting),
                "hint": (
                    'Allow them with POST /files/{id}/consent {"granted": true} '
                    "(the page shows an allow / don't-allow prompt) or remove them."
                ),
            },
        )
    return attachments, _file_refs(attachments)


async def _orchestrate_turn(
    settings: Any,
    payload: OrchestrateRequest,
    attachments: list[Any],
    files: list[dict[str, Any]],
    sessions: SessionMemory,
    *,
    run_id: str | None = None,
    sink: Any = None,
) -> tuple[int, dict[str, Any]]:
    """One Auto turn, as ``(status, body)`` — shared by the plain and live routes."""
    service = _orchestrator(settings)
    request = OrchestrationRequest(
        message=payload.message,
        history=_history_for(payload, sessions),
        attachments=attachments,
        agents=payload.agents,
        planner=payload.planner,
        synthesize=payload.synthesize,
        review=payload.review,
        evaluate=payload.evaluate,
    )
    try:
        result = await service.run(request, run_id=run_id, sink=sink)
    except ArasError as exc:
        # rare: agents turn a failed call into a `failed` outcome and the run
        # goes on — this is a failure outside any agent
        status, kind = _aras_failure(exc)
        logs.get_logger("orchestrator").warning("aras error: %s", exc)
        return _remembered_error_body(
            sessions, payload, mode="auto", status=status, kind=kind, exc=exc, files=files
        )

    logs.get_logger("orchestrator").info(
        "request done run=%s intent=%s agents=%s waves=%s parallel_ms=%s total_ms=%s "
        "synthesized=%s model_calls=%s quality=%s revised=%s",
        result.run_id,
        result.plan.intent,
        ",".join(result.plan.agents),
        len(result.waves),
        result.parallel_ms,
        result.latency_ms,
        result.synthesized,
        result.model_calls,
        result.evaluation.get("quality"),
        result.evaluation.get("revised"),
    )
    body = service.payload(result)
    body["session"] = _remember(
        sessions,
        payload,
        mode="auto",
        user_text=payload.message,
        files=files,
        response=body,
        run_id=result.run_id,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
    )
    return 200, body


@router.post("/orchestrate")
async def orchestrate(payload: OrchestrateRequest) -> Any:
    """Auto mode: manager plans, agents run in parallel, manager writes the answer."""
    settings = get_settings()
    prepared = _orchestration_inputs(payload)
    if isinstance(prepared, JSONResponse):
        return prepared
    attachments, files = prepared
    status, body = await _orchestrate_turn(
        settings, payload, attachments, files, get_memory()
    )
    if status != 200:
        return JSONResponse(status_code=status, content=body)
    return body


# ------------------------------------------------------------- live stream
def _sse(event: str, data: Any) -> str:
    """One server-sent event. ``\n\n`` ends the frame; the client splits on it."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _project(event: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Project one trace event, never raising.

    A thought is decoration; the run is the product. If an event carries a shape
    the projection did not expect, the watcher gets the plain line (who + what)
    and a warning goes to the log — the stream keeps going and the answer still
    arrives. This is the counterpart of the ``try/except`` around ``trace.sink``.
    """
    try:
        return live_thought(event)
    except Exception:  # noqa: BLE001 - a watcher must never break a run
        logs.get_logger("orchestrator").warning(
            "stream run=%s: could not project trace event %r (%s)",
            run_id,
            event.get("title"),
            event.get("stage"),
            exc_info=True,
        )
        return plain_thought(event)


def _reap_stream_run(task: asyncio.Task) -> None:
    """A run whose reader left still finishes; keep its outcome out of the void."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logs.get_logger("orchestrator").exception(
            "streamed run failed after its reader left", exc_info=error
        )


async def _stream_run(
    settings: Any,
    payload: OrchestrateRequest,
    attachments: list[Any],
    files: list[dict[str, Any]],
    sessions: SessionMemory,
    run_id: str,
    channel: Any,
    sink: Any,
):
    """Run the turn and relay its trace as it is written.

    The run itself is untouched: same manager, same trace, same record. The
    stream only *observes* it, so a client that disconnects mid-run costs nothing
    but the socket — the run finishes and is stored in memory as usual.
    """
    task = asyncio.create_task(
        _orchestrate_turn(
            settings, payload, attachments, files, sessions, run_id=run_id, sink=sink
        )
    )
    last_ping = 0.0
    try:
        yield _sse("open", {"run_id": run_id, "threshold_ms": 0})
        while True:
            # 250 ms is the shortest wait that still lets a cancel through on
            # the way out; a quiet stretch is a keep-alive, not a stall
            event = await channel.next(timeout=0.25)
            if event is not None:
                yield _sse("thought", _project(event, run_id))
                last_ping = 0.0
                continue
            if task.done():
                # nothing queued and nothing left to write: the run is over
                break
            # a quiet stretch: keep the socket warm so proxies do not drop it
            now = time.monotonic()
            if now - last_ping > 10.0:
                last_ping = now
                yield ": ping\n\n"
        try:
            status, body = await task
        except Exception as exc:  # noqa: BLE001 - report it in the stream, never hang
            logs.get_logger("orchestrator").exception("streamed run failed")
            status, body = 500, {
                "error": "internal",
                "message": f"The run failed: {type(exc).__name__}: {exc}",
                "hint": "The trace above shows how far it got; logs/error.log has the traceback.",
            }
        yield _sse("error" if status >= 400 else "result", body)
    finally:
        if not task.done():
            # The reader went away. Two ways to react, and the choice matters:
            # cancelling would throw away work the user already paid for, so the
            # run is left to finish on its own — it still writes its record and
            # still remembers the turn. Closing the channel makes the rest of
            # its events cheap no-ops.
            task.add_done_callback(_reap_stream_run)
        BUS.close(run_id)
        BUS.forget(run_id)


@router.post("/orchestrate/stream")
async def orchestrate_stream(payload: OrchestrateRequest) -> Any:
    """Same turn as ``POST /orchestrate``, streamed as it happens.

    ``text/event-stream`` frames:

    ``open``    the run id, sent before any work starts
    ``thought`` one trace event (planner, agent, tool, evaluator, manager), see
                ``app/orchestrator/live.py`` for the shape
    ``result``  the finished payload — byte for byte what ``POST /orchestrate``
                returns, including ``session``
    ``error``   the failure body, when the run could not finish

    Validation errors (unknown file, missing consent, orchestrator disabled) are
    still plain JSON with their status code: nothing is streamed for a request
    that never started.
    """
    settings = get_settings()
    prepared = _orchestration_inputs(payload)
    if isinstance(prepared, JSONResponse):
        return prepared
    attachments, files = prepared

    run_id = new_run_id()
    channel = BUS.open(run_id)
    sink = channel.publish
    return StreamingResponse(
        _stream_run(
            settings, payload, attachments, files, get_memory(), run_id, channel, sink
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx: do not buffer the stream
        },
    )


# ---------------------------------------------------------------- self-audit
def _audit_dir() -> Path:
    """Where scorecards live: ``<logs>/audit``, beside the run records."""
    return logs.log_dir() / "audit"


def _describe(settings: Any) -> dict[str, Any]:
    """What this host is wired to, never raising.

    Offline checks need no port at all, and a host whose ARAS block is
    incomplete must still be able to audit itself — so a failure to build the
    ports is reported as "not ready", not as an error that hides the audit.
    """
    try:
        return _orchestrator(settings).describe()
    except Exception as exc:  # noqa: BLE001 - reported as a finding, not a crash
        return {"model_ready": False, "search_ready": False, "error": f"{type(exc).__name__}: {exc}"}


def _audit_ports(settings: Any) -> tuple[Any, Any]:
    """The live ports for the opt-in checks; ``(None, None)`` when nothing is wired."""
    describe = _describe(settings)
    if not describe.get("model_ready"):
        return None, None
    try:
        models = build_model(settings)
    except Exception:  # noqa: BLE001 - a broken model port is exactly what the audit reports
        return None, None
    search = None
    if describe.get("search_ready"):
        try:
            search = build_search(settings)
        except Exception:  # noqa: BLE001 - live search checks degrade to "no search"
            search = None
    return models, search


async def _audit_and_save(
    settings: Any,
    *,
    live: bool,
    only: list[str] | None = None,
    on_event: Any = None,
) -> dict[str, Any]:
    """Run the audit, write the scorecard, and return it with the file paths."""
    models, search = _audit_ports(settings) if live else (None, None)
    scorecard = await run_audit(
        settings,
        live=live,
        models=models,
        search=search,
        only=only or None,
        on_event=on_event,
        version=VERSION,
    )
    scorecard["files"] = write_audit(scorecard, _audit_dir())
    scorecard["live_available"] = bool(_describe(settings).get("model_ready"))
    return scorecard


@router.get("/audit")
async def audit_catalogue_route() -> dict[str, Any]:
    """What the self-audit checks, plus the last scorecard.

    Static and free: the page calls this on load to label the button ("18 checks,
    last run passed") without running anything.
    """
    settings = get_settings()
    describe = _describe(settings)
    checks = audit_catalogue()
    return {
        "version": VERSION,
        "total": len(checks),
        "checks": checks,
        "levels": [{"id": level, "title": title} for level, title in AUDIT_LEVELS],
        "last": load_audit(_audit_dir()),
        "live_available": bool(describe.get("model_ready")),
        "environment": {
            "model_ready": bool(describe.get("model_ready")),
            "search_ready": bool(describe.get("search_ready")),
            "planner": str(getattr(settings, "orchestrator_planner", "")),
            "evaluate": bool(getattr(settings, "orchestrator_evaluate", False)),
            "review": bool(getattr(settings, "orchestrator_review", False)),
        },
    }


@router.post("/audit/run")
async def audit_run(payload: AuditRequest | None = None) -> Any:
    """Run every check and return the scorecard (JSON, plus a markdown twin on disk).

    Offline by default: the checks drive the real pipeline through scripted ports,
    so this needs no key, no network and no patience — it finishes in well under a
    second. ``live: true`` adds the two checks that call a real model, and those
    report ``skip`` (with the reason) when no provider is configured.
    """
    settings = get_settings()
    request = payload or AuditRequest()
    return await _audit_and_save(settings, live=request.live, only=request.only)


@router.get("/audit/last")
async def audit_last() -> Any:
    """The newest scorecard on disk — the audit trail a version can be judged by."""
    latest = load_audit(_audit_dir())
    if latest is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "no_audit",
                "message": "No self-audit has run yet.",
                "hint": "POST /audit/run, or press Evaluate in the page.",
            },
        )
    return latest


async def _stream_audit(settings: Any, *, live: bool, only: list[str]) -> Any:
    """Run the audit and relay each check as it finishes.

    Frames: ``open`` (how many checks), ``start`` (one check began), ``check``
    (its verdict, metrics and findings), ``result`` (the whole scorecard — the
    same body ``POST /audit/run`` returns), ``error`` when the audit itself fails.
    """
    queue: asyncio.Queue = asyncio.Queue()

    def publish(event: dict[str, Any]) -> None:
        queue.put_nowait(event)

    task = asyncio.create_task(
        _audit_and_save(settings, live=live, only=only, on_event=publish)
    )
    last_ping = 0.0
    try:
        yield _sse("open", {"total": len(audit_catalogue()), "live": live})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                event = None
            if event is not None:
                if event.get("type") == "done":
                    # the scorecard travels in the result frame, not as a duplicate
                    continue
                body = {key: value for key, value in event.items() if key != "type"}
                yield _sse(str(event.get("type") or "check"), body)
                last_ping = 0.0
                continue
            if task.done():
                break
            now = time.monotonic()
            if now - last_ping > 10.0:
                last_ping = now
                yield ": ping\n\n"
        try:
            scorecard = await task
        except Exception as exc:  # noqa: BLE001 - the stream must end, not hang
            logs.get_logger("orchestrator").exception("self-audit failed")
            yield _sse(
                "error",
                {
                    "error": "internal",
                    "message": f"The audit failed: {type(exc).__name__}: {exc}",
                    "hint": "logs/error.log has the traceback.",
                },
            )
            return
        yield _sse("result", scorecard)
    finally:
        if not task.done():
            task.add_done_callback(_reap_stream_run)


@router.get("/audit/stream")
async def audit_stream(live: bool = False, only: str = "") -> Any:
    """The audit, streamed check by check (``text/event-stream``).

    The page uses this so the list fills in as the checks finish instead of
    freezing on a spinner; the final frame is the same scorecard as the POST.
    """
    checks = [item.strip() for item in only.split(",") if item.strip()]
    return StreamingResponse(
        _stream_audit(get_settings(), live=live, only=checks),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs")
async def runs(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """The orchestration log base: what ran, in which order, and how it ended."""
    items = runlog.list_runs(limit)
    return {
        "count": len(items),
        "runs": items,
        "dir": str(runlog.runs_dir()),
        "hint": "Full trace: GET /runs/{run_id}, or python -m scripts.logs runs --run <id>",
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: str) -> Any:
    record = runlog.get_run(run_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "message": f"no orchestration run '{run_id}'",
                "hint": "GET /runs lists what is on disk (logs/orchestrator/).",
            },
        )
    return record


@router.get("/sessions")
async def sessions_list(
    limit: int = Query(default=20, ge=1, le=200),
    q: str = Query(default="", description="filter on title, last message, last answer"),
) -> dict[str, Any]:
    """The harness memory: past conversations, newest first."""
    sessions = get_memory()
    items = sessions.list(limit, q)
    return {
        "enabled": sessions.enabled,
        "count": len(items),
        "sessions": items,
        "dir": str(sessions.dir),
        "writable": sessions.writable,
        "hint": (
            "GET /sessions/{id} replays one turn by turn; POST /orchestrate (or /chat) "
            "with its session_id continues it."
        ),
    }


@router.post("/sessions")
async def sessions_create(payload: SessionCreateRequest) -> Any:
    """Start a conversation before the first message (optional — the first turn creates one)."""
    sessions = get_memory()
    if not sessions.enabled:
        return JSONResponse(
            status_code=503,
            content={
                "error": "disabled",
                "message": "Harness memory is off (HARNESS_MEMORY=false).",
                "hint": "Set HARNESS_MEMORY=true in .env to keep conversations.",
            },
        )
    record = sessions.create(title=payload.title, mode=payload.mode)
    return session_view(record, history_turns=sessions.history_turns)


@router.get("/sessions/{session_id}")
async def sessions_get(session_id: str) -> Any:
    """One conversation, every turn with the payload the page rendered."""
    sessions = get_memory()
    record = sessions.get(session_id)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "not_found",
                "message": f"no session '{session_id}'",
                "hint": "GET /sessions lists what is in memory; memory/ holds the JSON files.",
            },
        )
    return session_view(record, history_turns=sessions.history_turns)


@router.patch("/sessions/{session_id}")
async def sessions_rename(session_id: str, payload: SessionRenameRequest) -> Any:
    sessions = get_memory()
    record = sessions.rename(session_id, payload.title)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"no session '{session_id}'"},
        )
    return session_view(record, history_turns=sessions.history_turns)


@router.delete("/sessions/{session_id}")
async def sessions_delete(session_id: str) -> Any:
    sessions = get_memory()
    if not sessions.delete(session_id):
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": f"no session '{session_id}'"},
        )
    logs.get_logger("memory").info("session forgotten: %s", session_id)
    return {"deleted": session_id}


@router.get("/aras")
async def aras_status() -> dict[str, Any]:
    """The ARAS microservices this host runs on: config + live health.

    No secrets: endpoints and readiness only. ``checked`` carries one health
    probe per configured service (the unauthenticated /health route each
    service exposes), so this answers "is the tunnel up" in one call.
    ``model_ready`` / ``search_ready`` say whether the router and the web
    service have everything they need, and ``missing`` names what does not.
    """
    settings = get_settings()
    report = await aras.status(settings)
    report["version"] = VERSION
    report["endpoints"] = {
        name: settings.aras_endpoint(name) for name in settings.aras_services_configured
    }
    return report


@router.get("/aras/models")
async def aras_models() -> Any:
    """The model ids the router lists — where a value for ``ARAS_MODEL`` comes from.

    Needs the router URL + token only, so it works before a model is chosen.
    """
    settings = get_settings()
    try:
        models = await aras.ArasRouterClient(settings).models()
    except ArasError as exc:
        status, kind = _aras_failure(exc)
        logs.get_logger("api").warning("router model list failed: %s", exc)
        return _error(status, kind, exc)
    return {
        "provider": aras.PROVIDER,
        "endpoint": settings.aras_endpoint("router"),
        "configured_model": settings.aras_model_id,
        "count": len(models),
        "models": models,
    }


async def _nothing() -> None:
    return None


@router.get("/diagnose")
async def diagnose(
    q: str = Query(default="python asyncio tutorial"),
) -> dict[str, Any]:
    """One-stop verification: a real model call, a real search, every /health.

    Three layers, run together: the router answers a completion (URL + token +
    ``ARAS_MODEL``), the web service answers a query, and every configured
    service answers its health route. ``hints`` turns the result into the next
    thing to do.
    """
    settings = get_settings()
    model = build_model(settings)
    search = build_search(settings)

    model_report, search_report, aras_report = await asyncio.gather(
        model.ping(),
        search.ping(q) if search else _nothing(),
        aras.status(settings) if settings.aras_services_configured else _nothing(),
    )
    checked = [search_report] if search_report is not None else []
    skipped = (
        []
        if search
        else [
            {
                "provider": aras.PROVIDER,
                "reason": "not configured — missing " + ", ".join(search.missing),
            }
        ]
    )

    return {
        "version": VERSION,
        "model": model_report,
        "search": {
            "checked": checked,
            "skipped": skipped,
            "ok": [r["provider"] for r in checked if r.get("ok")],
            "failed": [r["provider"] for r in checked if not r.get("ok")],
        },
        "aras": aras_report,
        "hints": _hints(settings, model_report, checked, aras_report),
    }


def _hints(
    settings,
    model: dict[str, Any],
    search: list[dict[str, Any]],
    aras_report: dict[str, Any] | None = None,
) -> list[str]:
    """The next thing to do, most fundamental first: file, config, tunnel, calls."""
    hints: list[str] = []
    if not Path(".env").exists():
        hints.append("No .env file found; copy .env.example to .env and fill the ARAS block.")

    model_missing = settings.aras_missing("router")
    if model_missing:
        hints.append(
            "The model is not configured: set " + ", ".join(model_missing) + " in .env. "
            "The ARAS router is the only model this app has, so no agent can write an answer until then."
        )
    search_missing = settings.aras_missing("web")
    if search_missing:
        hints.append(
            "Web search is not configured: set " + ", ".join(search_missing) + " in .env. "
            "The ARAS web service is the only search backend, so the search agent cannot search."
        )

    down_names: set[str] = set()
    if aras_report:
        checked = aras_report.get("checked", [])
        down = [c for c in checked if not c.get("ok")]
        down_names = {str(c.get("service")) for c in down}
        if down:
            names = ", ".join(c["service"] for c in down)
            answered = [c for c in down if str(c.get("error", "")).startswith("HTTP")]
            if answered and len(answered) == len(down):
                # every failure is an HTTP *response* — something replied, so
                # the tunnel is not simply closed: usually a VPN/system proxy
                # intercepting loopback (fixed in-app via trust_env=False, but
                # a TUN-level VPN can still capture 127.0.0.1)
                hints.append(
                    f"ARAS service(s) answered with HTTP errors: {names}. Something is "
                    "replying, so the tunnel is not closed — a VPN or system proxy may be "
                    "intercepting 127.0.0.1. Unset HTTP_PROXY/HTTPS_PROXY (or add "
                    "127.0.0.1 to NO_PROXY), then compare with a plain terminal: "
                    "curl http://127.0.0.1:8101/api/v1/health/"
                )
            else:
                hints.append(
                    f"ARAS service(s) not answering: {names}. Is the SSH tunnel "
                    "open? (ssh -L <port>:127.0.0.1:<port> ... — see GET /aras for detail, "
                    "or run python scripts/aras_smoke.py)"
                )
        if not aras_report.get("rag_ready") and "retrieval" in aras_report.get("configured", []):
            hints.append(
                "ARAS retrieval is configured but ARAS_COLLECTION is empty, so the "
                "document agent cannot retrieve — name the collection (e.g. alireza_notes)."
            )
        if not aras_report.get("data_ready") and "data" in aras_report.get("configured", []):
            hints.append(
                "ARAS data is configured but needs ARAS_TENANT + ARAS_OWNER + "
                "ARAS_DATA_SOURCE_ID before the data agent can query it."
            )

    # A call that failed although its service is configured and answers /health:
    # the fix is in the request (model id, token scope), not in the tunnel.
    if not model_missing and not model.get("ok") and "router" not in down_names:
        error = model.get("error") or {}
        why = str(error.get("hint") or error.get("message") or "").strip()
        hints.append(
            "The router did not answer the test completion"
            + (f": {why}" if why else "")
            + ". GET /aras/models lists the model ids it serves."
        )
    if search and all(not r.get("ok") for r in search) and "web" not in down_names:
        error = search[0].get("error") or {}
        why = str(error.get("hint") or error.get("message") or "").strip()
        hints.append("The web service did not answer the test search" + (f": {why}" if why else "") + ".")
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
