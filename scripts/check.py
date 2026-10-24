#!/usr/bin/env python3
"""
scripts/check.py
================

The final quality gate: runs the complete test suite and prints a
human-readable report.

    python3 scripts/check.py

Exit code 0 → everything passed; non-zero → at least one check failed.
"""

from __future__ import annotations

from importlib import metadata
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║   Agetnic Orchestration Sandbox — final check                ║
║   Tests: config · LLM · planner · executor · manager ·       ║
║           agents · RAG · router · traces · HTTP API          ║
╚══════════════════════════════════════════════════════════════╝
"""


def _configure_utf8_output() -> None:
    """Ensure Unicode banners/status symbols print cleanly on Windows."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


def _check_test_dependencies() -> None:
    """Warn softly if older global test packages are shadowing requirements."""
    notices: list[str] = []

    try:
        pa_ver = metadata.version("pytest-asyncio")
        major = int(pa_ver.split(".", 1)[0])
        if major < 1:
            notices.append(f"pytest-asyncio {pa_ver} (< 1.0)")
    except (metadata.PackageNotFoundError, ValueError):
        pass

    try:
        metadata.version("httpx2")
    except metadata.PackageNotFoundError:
        notices.append("httpx2 missing (needed by Starlette TestClient)")

    if notices:
        joined = ", ".join(notices)
        print(
            f"ℹ️   Outdated/missing test packages detected ({joined}).\n"
            f"    Run: {sys.executable} -m pip install -U -r requirements.txt\n",
            flush=True,
        )


def main() -> int:
    _configure_utf8_output()
    print(BANNER, flush=True)
    _check_test_dependencies()
    started = time.perf_counter()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
        cwd=ROOT,
    )

    elapsed = time.perf_counter() - started
    print()
    if result.returncode == 0:
        print(f"✅  ALL CHECKS PASSED  ({elapsed:.1f}s)")
    else:
        print(f"❌  CHECKS FAILED  ({elapsed:.1f}s) — see output above")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
