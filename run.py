#!/usr/bin/env python3
"""Start the Agetnic Orchestration Sandbox, with real logs.

One process serves **both** the test harness and the API, which is what keeps
``POST /chat`` from returning ``Not Found``: the page must come from FastAPI,
not from a static file server or a raw ``index.html``.

    python run.py                    # http://localhost:8000
    python run.py --port 9000
    python run.py --reload           # auto-reload while editing
    python run.py --log-level DEBUG  # more detail in logs/agetnic.log
    python run.py --no-terminal-log  # don't mirror stdout/stderr into logs/terminal.log

Everything printed to the terminal (uvicorn's own lines included) is mirrored
into ``logs/terminal.log``; app events, errors with tracebacks, per-request
lines and process data go to the other files described in ``logs/README.md``.

The API is reachable both with and without the ``/api`` prefix
(``/chat`` and ``/api/chat``), so proxies that add a prefix keep working.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from app import logs
from app.config import get_settings
from app.main import API_ROUTES, ASSETS_DIR, INDEX_FILE
from app.version import VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the orchestration sandbox (UI + API).")
    parser.add_argument("--host", default=None, help="bind address (default: HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default: PORT or 8000)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on file changes")
    parser.add_argument("--log-dir", default=None, help="where to write logs (default: logs/ or LOG_DIR)")
    parser.add_argument(
        "--log-level",
        default=None,
        help="DEBUG | INFO | WARNING | ERROR (default: LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--no-terminal-log",
        action="store_true",
        help="do not mirror the terminal output into logs/terminal.log",
    )
    args = parser.parse_args()

    # 1. mirror the terminal first, so the console handler below lands in it too
    terminal = None if args.no_terminal_log else logs.capture_terminal(args.log_dir)

    # 2. files + console, redacting secrets
    summary = logs.setup_logging(args.log_dir, level=args.log_level, force=True)
    logs.install_exception_hooks()
    crash = logs.enable_faulthandler(args.log_dir)
    log = logs.get_logger("run")

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
    if not summary["writable"]:
        print(f"  ! logs are not writable at {summary['dir']} — console logging only")
    else:
        print(f"  logs     : {summary['dir']}/")
        print("             agetnic.log (everything) · error.log (failures) ·")
        print("             requests.log (one line per HTTP call) · process.log ·")
        if terminal:
            print(f"             terminal.log (mirror of this window) · crash.log")
        print("  tail     : python -m scripts.logs tail        # or: errors / requests")
    print("-" * 68)

    logs.record_startup(
        VERSION,
        start="run.py",
        host=host,
        port=port,
        ui=base,
        problems=problems,
        terminal_log=str(terminal) if terminal else None,
        crash_log=str(crash) if crash else None,
    )
    for problem in problems:
        log.warning("preflight: %s", problem)
    log.info("starting uvicorn host=%s port=%s reload=%s", host, port, args.reload)

    import uvicorn

    # log_config=None keeps uvicorn from installing its own handlers: its loggers
    # propagate to ours, so uvicorn lines reach agetnic.log/error.log too.
    # uvicorn's per-request access line is redundant (requests.log is richer).
    logging.getLogger("uvicorn.access").disabled = True
    try:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=args.reload,
            log_config=None,
            log_level=str(summary["level"]).lower(),
        )
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        log.info("stopped pid=%s", os.getpid())
        logging.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
