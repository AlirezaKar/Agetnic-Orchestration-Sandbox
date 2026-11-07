#!/usr/bin/env python3
"""The self-audit, from the command line — the same checks the Evaluate button runs.

No key, no network, no patience needed: every offline check drives the real
pipeline through its four ports with scripted models, search and agent loops, so
a red line always means the code changed.

    python -m scripts.audit                 # offline, prints a table, exits 1 on a failure
    python -m scripts.audit --json          # the scorecard on stdout
    python -m scripts.audit --live          # also the two checks that call a model
    python -m scripts.audit --only gate.rules,plan.waves
    python -m scripts.audit --no-save       # do not write logs/audit/

Exit status: 0 pass or warnings only, 1 any failure, 2 the audit itself broke.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app import logs
from app.config import get_settings
from app.orchestrator import audit_catalogue, run_audit, write_audit
from app.version import VERSION

ICON = {"pass": "ok  ", "warn": "warn", "fail": "FAIL", "skip": "skip"}


def _live_ports(settings):
    """The host's real ports, only when a model is actually configured."""
    try:
        from app.llm.registry import build_chain
        from app.search.registry import build_search_chain
    except Exception:  # pragma: no cover - a stripped checkout
        return None, None
    try:
        models = build_chain(settings)
    except Exception:  # noqa: BLE001 - "no key" is a normal answer here
        return None, None
    try:
        search = build_search_chain(settings)
    except Exception:  # noqa: BLE001 - live search checks then degrade
        search = None
    return models, search


def _print_table(scorecard: dict) -> None:
    summary = scorecard.get("summary") or {}
    print(
        f"self-audit · v{scorecard.get('version') or VERSION} · "
        f"{summary.get('pass', 0)} pass / {summary.get('warn', 0)} warn / "
        f"{summary.get('fail', 0)} fail / {summary.get('skip', 0)} skip · "
        f"{scorecard.get('ms', 0)} ms"
    )
    level = ""
    for row in scorecard.get("checks") or []:
        if row["level"] != level:
            level = row["level"]
            print(f"\n  {level}")
        print(f"    {ICON.get(row['status'], row['status'])}  {row['id']:<20} {row['ms']:>5} ms  {row['detail']}")
        for finding in row.get("findings") or []:
            print(f"            - {finding}")
    files = scorecard.get("files") or {}
    if files.get("json"):
        print(f"\n  scorecard: {files['json']}")
    print(f"  status: {scorecard.get('status')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the orchestration itself.")
    parser.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    parser.add_argument("--live", action="store_true", help="include the checks that call a model")
    parser.add_argument("--only", default="", help="comma-separated check ids")
    parser.add_argument("--no-save", action="store_true", help="do not write logs/audit/")
    parser.add_argument("--list", action="store_true", help="print the catalogue and exit")
    args = parser.parse_args(argv)

    if args.list:
        for spec in audit_catalogue():
            mark = " (live)" if spec["live"] else ""
            print(f"{spec['level']:<8} {spec['id']:<20} {spec['title']}{mark}")
            print(f"{'':<30}{spec['proves']}")
        return 0

    settings = get_settings()
    models, search = _live_ports(settings) if args.live else (None, None)
    only = [item.strip() for item in args.only.split(",") if item.strip()]
    try:
        scorecard = asyncio.run(
            run_audit(
                settings,
                live=args.live,
                models=models,
                search=search,
                only=only or None,
                version=VERSION,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the audit itself broke
        print(f"the audit could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not args.no_save:
        folder = logs.log_dir() / "audit"
        scorecard["files"] = write_audit(scorecard, folder)

    if args.json:
        print(json.dumps(scorecard, indent=2, ensure_ascii=False))
    else:
        _print_table(scorecard)

    status = scorecard.get("status")
    if status == "fail":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
