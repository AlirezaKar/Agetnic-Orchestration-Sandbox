#!/usr/bin/env python3
"""Start the Agetnic Orchestration Sandbox correctly.

One process serves **both** the test harness and the API, which is what keeps
``POST /chat`` from returning ``Not Found``: the page must come from FastAPI,
not from a static file server or a raw ``index.html``.

    python run.py                 # http://localhost:8000
    python run.py --port 9000
    python run.py --reload        # auto-reload while editing

The API is reachable both with and without the ``/api`` prefix
(``/chat`` and ``/api/chat``), so proxies that add a prefix keep working.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import applog
from app.config import get_settings
from app.main import API_ROUTES, ASSETS_DIR, INDEX_FILE, VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the orchestration sandbox (UI + API).")
    parser.add_argument("--host", default=None, help="bind address (default: HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default: PORT or 8000)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on file changes")
    args = parser.parse_args()

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    problems: list[str] = []
    if not INDEX_FILE.exists():
        problems.append(f"missing UI file: {INDEX_FILE}")
    if not ASSETS_DIR.exists():
        problems.append(f"missing assets folder: {ASSETS_DIR} (the UI would load unstyled)")
    if not Path(".env").exists():
        problems.append("no .env file — copy .env.example to .env and add one provider key")

    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    base = f"http://{display_host}:{port}"

    print("-" * 68)
    print(f"  Agetnic Orchestration Sandbox  v{VERSION}")
    print(f"  UI + API : {base}/")
    print(f"  API also : {base}/api/…")
    print(f"  Docs     : {base}/docs")
    print(f"  Bind     : {host}:{port}")
    for problem in problems:
        print(f"  ! {problem}")
    print("-" * 68)
    print("  routes: " + ", ".join(route.split()[1] for route in API_ROUTES[:8]))
    if problems:
        print("  (the server still starts; fix the notes above for a complete setup)")

    log_path = applog.append(
        VERSION, applog.startup_record(VERSION, host, port, base) + f" problems={len(problems)}"
    )
    if log_path:
        print(f"  log      : {log_path.relative_to(Path(__file__).resolve().parent)}")

    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=args.reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
