"""
backend.api.routes
==================

REST endpoints consumed by the HTML/JS test harness:

``GET  /api/health``         provider + configuration status
``POST /api/chat``           run one orchestrated turn (the core endpoint)
``GET  /api/modes``          available modes & agents (for the UI)
``POST /api/documents``      upload a text file into the RAG store
``GET  /api/documents``      list indexed documents
``GET  /api/traces``         list past run summaries (newest first)
``GET  /api/traces/{id}``    full trace JSON of one run
``DELETE /api/sessions/{id}`` clear conversation memory of a session
"""

from __future__ import annotations

import time
from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..agents.registry import get_agents, get_document_store
from ..config import settings
from ..orchestration.manager import Manager
from ..orchestration.session import session_store
from ..orchestration.trace import list_trace_summaries, load_trace
from ..schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    Mode,
    RunTrace,
)

router = APIRouter(prefix="/api")

# File uploads for the Document agent are capped (sandbox safety).
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB


# ──────────────────────────────────────────────────────────────────
#  Health / metadata
# ──────────────────────────────────────────────────────────────────
@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness + which providers would be used right now."""
    from ..search import get_page_fetcher

    fetcher = get_page_fetcher()
    return HealthResponse(
        status="ok",
        llm_provider=settings.effective_llm_provider,
        web_search_provider=settings.web_search_provider,
        page_fetcher=fetcher.name if fetcher else "none",
        models={
            "orchestrator": settings.groq_model_orchestrator,
            "agent": settings.groq_model_agent,
        },
        trace_dir=str(settings.trace_dir),
        document_count=get_document_store().document_count,
    )


@router.get("/modes")
async def modes() -> dict:
    """Machine-readable catalogue of modes/agents for the UI."""
    agents = get_agents()
    return {
        "modes": [
            {
                "id": Mode.AUTO.value,
                "label": "Auto (Manager + Planner)",
                "description": "Any prompt is analysed and delegated to the right agent(s), in parallel where possible.",
            },
            {
                "id": Mode.DOCUMENT.value,
                "label": "Document",
                "description": "File handling & RAG-grounded chat over uploaded documents.",
            },
            {
                "id": Mode.WEB_SEARCH.value,
                "label": "Web Search",
                "description": "SearXNG metasearch + Crawl4AI deep-crawl, answered grounded in the results.",
            },
            {
                "id": Mode.GENERAL.value,
                "label": "General",
                "description": "General LLM chat, conversation and knowledge.",
            },
            {
                "id": Mode.NUMBER.value,
                "label": "Number",
                "description": "Data and complex information analysis.",
            },
        ],
        "agents": [
            {"id": agent.name.value, "description": agent.description}
            for agent in agents.values()
        ],
    }


# ──────────────────────────────────────────────────────────────────
#  Core orchestration endpoint
# ──────────────────────────────────────────────────────────────────
@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Run one orchestrated turn.

    The Manager decides how the goal is reached (auto mode) or delegates
    to the pinned agent (manual modes).  The response carries the final
    answer *and* the full orchestration timeline; the complete debug
    record is persisted under ``traces/``.
    """
    manager = Manager(
        llm=_orchestrator_llm(),
        agents=get_agents(),
    )
    started = time.perf_counter()
    try:
        response = await manager.run(request)
    except Exception as exc:  # noqa: BLE001 — surface as HTTP 500 with detail
        raise HTTPException(
            status_code=500,
            detail=f"Orchestration failed: {type(exc).__name__}: {exc}",
        ) from exc
    response.metrics["api_duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return response


def _orchestrator_llm():
    from ..llm import get_orchestrator_llm

    return get_orchestrator_llm()


# ──────────────────────────────────────────────────────────────────
#  Document upload (RAG corpus)
# ──────────────────────────────────────────────────────────────────
@router.post("/documents")
async def upload_document(file: UploadFile = File(...)) -> dict:
    """
    Upload a text document into the local RAG store.

    Accepts ``.txt`` / ``.md`` (plain UTF-8 text).  The store chunk-ids
    and indexes it immediately — the Document agent can use it in the
    very next turn.
    """
    filename = file.filename or "unnamed.txt"
    if not filename.lower().endswith((".txt", ".md")):
        raise HTTPException(
            status_code=400,
            detail="Only .txt and .md files are supported in this sandbox.",
        )

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 2 MB sandbox limit.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text.") from exc

    meta = get_document_store().add_document(filename, text)
    return {"status": "indexed", **meta}


@router.get("/documents")
async def list_documents() -> dict:
    store = get_document_store()
    return {
        "documents": store.list_documents(),
        "chunk_count": store.chunk_count,
    }


# ──────────────────────────────────────────────────────────────────
#  Trace inspection (evaluation & debugging)
# ──────────────────────────────────────────────────────────────────
@router.get("/traces")
async def traces(limit: int = 50) -> dict:
    """Run summaries (newest first) for the debug panel."""
    return {"traces": list_trace_summaries(limit=min(limit, 200))}


@router.get("/traces/{run_id}", response_model=RunTrace)
async def trace_detail(run_id: str) -> RunTrace:
    """The complete persisted trace of one run (input/thinking/process/output)."""
    trace = load_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Trace '{run_id}' not found.")
    return trace


# ──────────────────────────────────────────────────────────────────
#  Sessions
# ──────────────────────────────────────────────────────────────────
@router.delete("/sessions/{session_id}")
async def clear_session(session_id: str) -> dict:
    """Forget a conversation (the user can start clean)."""
    session_store.clear(session_id)
    return {"status": "cleared", "session_id": session_id}
