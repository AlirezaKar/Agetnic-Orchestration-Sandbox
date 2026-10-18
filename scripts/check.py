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


def main() -> int:
    print(BANNER)
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
