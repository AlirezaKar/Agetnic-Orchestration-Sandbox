"""
backend.api.app
===============

FastAPI application factory for the **test harness**:

* serves the static HTML/CSS/JS frontend from ``frontend/``
* mounts the REST API under ``/api``
* enables permissive CORS (the sandbox is a local evaluation tool)

The frontend exists for one purpose only — exercising the orchestrator
and visualising its thinking/process — so the UI is plain static files
with no build step.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import settings
from .routes import router as api_router

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def create_app() -> FastAPI:
    """Application factory (used by ``run_server.py`` and the tests)."""
    app = FastAPI(
        title="Agetnic Orchestration Sandbox",
        description=(
            "Test harness for a Manager-rooted multi-agent orchestration "
            "architecture: Planner, parallel Executor, specialist agents, "
            "and full JSON traces of every run."
        ),
        version=__version__,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # REST API first …
    app.include_router(api_router)

    # … then the static test UI at "/".
    if FRONTEND_DIR.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=FRONTEND_DIR / "assets"),
            name="assets",
        )

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

    return app
