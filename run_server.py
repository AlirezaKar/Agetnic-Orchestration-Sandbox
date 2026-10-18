#!/usr/bin/env python3
"""
run_server.py
=============

Entry point for the orchestration test harness.

    python3 run_server.py

Reads ``.env`` (see ``.env.example``), then serves:

* the HTML/CSS/JS test UI  →  http://localhost:8000/
* the REST API             →  http://localhost:8000/api/…
"""

from __future__ import annotations

import uvicorn

from backend.api import create_app
from backend.config import settings


def main() -> None:
    app = create_app()
    provider = settings.effective_llm_provider
    print("─" * 62)
    print("  Agetnic Orchestration Sandbox")
    print(f"  LLM provider : {provider}"
          + ("  (set GROQ_API_KEY in .env for real LLM calls)" if provider == "mock" else ""))
    print(f"  Web search   : {settings.web_search_provider}")
    print(f"  Traces       : {settings.trace_dir}")
    print(f"  UI + API     : http://{settings.host}:{settings.port}/")
    print("─" * 62)

    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
