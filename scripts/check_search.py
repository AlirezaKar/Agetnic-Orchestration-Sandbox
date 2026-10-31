#!/usr/bin/env python3
"""Verify the web-search mechanism without starting the web server.

Examples
--------
    # health of every backend in SEARCH_PROVIDERS (default: ddg, wikipedia)
    python -m scripts.check_search

    # also try backends that are not in the chain but have a key set
    python -m scripts.check_search --all

    # a real query through the whole chain, full results
    python -m scripts.check_search -q "python 3.13 release notes"

    # pin one backend
    python -m scripts.check_search --provider wikipedia -q "asyncio"

Exit code 0 means at least one backend answered; 1 means all failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app import logs
from app.config import get_settings
from app.search.base import SearchError
from app.search.presets import SEARCH_PRESETS
from app.search.providers import PROVIDER_CLASSES
from app.search.registry import build_search_chain, describe_search_providers

ARTIFACTS = Path("artifacts")


def _preset_missing_key(name: str, settings) -> list[str]:
    preset = SEARCH_PRESETS[name]
    return [env for env in preset.key_env if not getattr(settings, env.lower(), None)]


async def diagnose(names: list[str], query: str, as_json: bool) -> int:
    settings = get_settings()
    reports = []
    for name in names:
        if name not in PROVIDER_CLASSES:
            reports.append({"provider": name, "ok": False, "error": {"message": "unknown backend"}})
            continue
        missing = _preset_missing_key(name, settings)
        if missing:
            reports.append(
                {"provider": name, "ok": False, "skipped": True, "error": {"message": "missing " + ", ".join(missing)}}
            )
            continue
        reports.append(await PROVIDER_CLASSES[name](settings).ping(query))

    if as_json:
        print(json.dumps(reports, indent=2))
    else:
        print(f"\nsearch backends (query: {query!r})\n" + "-" * 72)
        for report in reports:
            name = report["provider"]
            if report.get("skipped"):
                print(f"  SKIP   {name:<12} {report['error']['message']}")
                continue
            if report.get("ok"):
                print(f"  PASS   {name:<12} {report.get('count', 0)} results in {report.get('latency_ms')} ms")
                for sample in report.get("sample", [])[:2]:
                    print(f"         - {sample['title'][:70]}")
                    print(f"           {sample['url'][:90]}")
            elif report.get("empty"):
                print(f"  EMPTY  {name:<12} responded but returned no results")
            else:
                error = report.get("error") or {}
                print(f"  FAIL   {name:<12} {error.get('message', 'unknown error')}")
                if error.get("hint"):
                    print(f"         hint: {error['hint']}")
        print("-" * 72)

    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "search-diagnose.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0 if any(r.get("ok") for r in reports) else 1


async def run_query(query: str, provider: str | None, max_results: int, as_json: bool) -> int:
    settings = get_settings()
    try:
        if provider:
            name = provider.lower()
            if name not in PROVIDER_CLASSES:
                print(f"unknown backend '{provider}'. known: {', '.join(sorted(PROVIDER_CLASSES))}")
                return 2
            results = await PROVIDER_CLASSES[name](settings).search(query, max_results)
            payload = {
                "query": query,
                "provider": name,
                "count": len(results),
                "results": [r.to_dict() for r in results],
            }
        else:
            chain = build_search_chain(settings)
            if chain.skipped:
                print("skipped backends: " + "; ".join(chain.skipped))
            response = await chain.search(query, max_results)
            payload = response.to_dict()
    except SearchError as exc:
        print(f"\nFAILED: {exc}")
        if exc.hint:
            print(f"hint: {exc.hint}")
        return 1

    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\nprovider: {payload.get('provider')}")
        print(f"results : {payload['count']}\n")
        for index, item in enumerate(payload["results"], start=1):
            print(f"{index}. {item['title']}")
            print(f"   {item['url']}")
            snippet = item.get("snippet") or ""
            print(f"   {snippet[:220]}{'…' if len(snippet) > 220 else ''}\n")
        for attempt in payload.get("attempts", []):
            if not attempt.get("ok"):
                print(f"  note: {attempt.get('provider')} -> {attempt.get('message', 'no results')}")

    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "search-last-query.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0 if payload["count"] else 1


def main() -> int:
    logs.setup_logging()
    parser = argparse.ArgumentParser(description="Verify the configured web-search backends.")
    parser.add_argument("-q", "--query", default="python asyncio tutorial")
    parser.add_argument("--provider", help="pin one backend instead of using the chain")
    parser.add_argument("--all", action="store_true", help="diagnose every known backend, not just the chain")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="machine readable output")
    args = parser.parse_args()

    settings = get_settings()
    if args.provider:
        return asyncio.run(run_query(args.query, args.provider, args.max_results, args.json))

    if args.all:
        names = list(PROVIDER_CLASSES)
    else:
        names = settings.search_chain
        print(f"chain: {' -> '.join(names)}")
        for entry in describe_search_providers(settings):
            if entry["is_active"] and not entry["configured"]:
                print(f"warning: {entry['name']} is in the chain but {', '.join(entry['api_key_envs']) or 'a key'} is missing")
    return asyncio.run(diagnose(names, args.query, args.json))


if __name__ == "__main__":
    sys.exit(main())
