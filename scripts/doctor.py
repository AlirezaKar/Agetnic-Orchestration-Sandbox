#!/usr/bin/env python3
"""One command that answers: "is my setup actually working?"

Checks, in order:
  1. is there a .env and which providers are configured
  2. does the core model answer (tiny completion)
  3. does every search backend answer (real query)
  4. optionally: a full agent run (model + web search + citations)

    python -m scripts.doctor
    python -m scripts.doctor --search-only
    python -m scripts.doctor --agent -q "latest python release" --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app import logs
from app.agent import WebSearchAgent
from app.config import get_settings
from app.llm.base import ProviderError
from app.llm.registry import ConfigurationError, build_chain, describe_providers
from app.search.base import SearchError
from app.search.presets import SEARCH_PRESETS
from app.search.providers import PROVIDER_CLASSES
from app.search.registry import build_search_chain
from app.version import VERSION

ARTIFACTS = Path("artifacts")
OK, BAD, SKIP = "PASS", "FAIL", "SKIP"


def skipped(reason: str) -> dict:
    """Explicit "this was not run" record — never a bare JSON null."""
    return {"skipped": True, "reason": reason}


def line(status: str, label: str, detail: str = "") -> None:
    print(f"  [{status}] {label:<24} {detail}")


async def check_model(settings) -> tuple[bool, list[dict]]:
    chain = build_chain(settings)
    reports: list[dict] = []
    print("\n1) core model")
    if not chain:
        line(BAD, "configured", "no provider has both a key and a model")
        for note in chain.skipped:
            print(f"         {note}")
        print("         -> pick one: python -m scripts.check_providers --links")
        if any("MODEL" in note for note in chain.skipped):
            print("         -> a key alone is not enough: fill <PROVIDER>_MODEL too")
        return False, reports
    for note in chain.skipped:
        print(f"         (skipped: {note})")
    all_ok = True
    for provider in chain.providers:
        report = await provider.ping()
        reports.append(report)
        if report["ok"]:
            line(OK, provider.name, f"{provider.model} · {report['latency_ms']} ms · {report['reply']!r}")
        else:
            all_ok = False
            error = report["error"]
            line(BAD, provider.name, error["message"])
            if error.get("hint"):
                print(f"         hint: {error['hint']}")
    return all_ok, reports


async def check_search(settings, query: str) -> tuple[bool, list[dict]]:
    print("\n2) web search")
    reports: list[dict] = []
    chain = build_search_chain(settings)
    if not chain:
        line(BAD, "configured", "no usable backend")
        for note in chain.skipped:
            print(f"         {note}")
        return False, reports
    for note in chain.skipped:
        print(f"         (skipped: {note})")
    any_ok = False
    for provider in chain.providers:
        report = await provider.ping(query)
        reports.append(report)
        if report.get("ok"):
            any_ok = True
            line(OK, provider.name, f"{report['count']} results · {report['latency_ms']} ms")
            for sample in report.get("sample", [])[:1]:
                print(f"         top hit: {sample['title'][:64]}")
        else:
            error = report.get("error") or {}
            line(BAD, provider.name, error.get("message", "no results"))
            if error.get("hint"):
                print(f"         hint: {error['hint']}")
    return any_ok, reports


async def check_agent(settings, question: str) -> tuple[bool, dict]:
    print("\n3) full agent run")
    chain = build_chain(settings)
    if not chain:
        line(SKIP, "agent", "no model configured")
        return False, {}
    agent = WebSearchAgent(chain, build_search_chain(settings), settings)
    try:
        result = await agent.run(question, max_steps=settings.agent_max_steps)
    except (ConfigurationError, ProviderError) as exc:
        line(BAD, "agent", str(exc))
        return False, {}
    tools = [t["name"] for t in result.trace if t["type"] == "tool"]
    line(OK if result.answer else BAD, "agent", f"{result.steps} steps · tools: {tools or 'none'}")
    line(OK if result.sources else SKIP, "sources", f"{len(result.sources)} url(s)")
    print("\n  answer:\n")
    print("    " + (result.answer or "(empty)").replace("\n", "\n    ")[:1500])
    return bool(result.answer), result.to_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify model + search configuration end to end.")
    parser.add_argument("-q", "--query", default="python asyncio tutorial", help="search query for the checks")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--agent", action="store_true", help="also run the full agent loop")
    parser.add_argument("--question", default=None, help="question for the agent run")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    logs.setup_logging()
    logs.install_exception_hooks()
    logs.record_startup(VERSION, start="scripts.doctor", argv=sys.argv)
    log = logs.get_logger("doctor")

    settings = get_settings()
    print("Agetnic Orchestration Sandbox — doctor")
    print("=" * 72)
    print(f"  .env present: {os.path.exists('.env')}")
    if settings.is_auto_provider:
        ready = [p["name"] for p in describe_providers(settings) if p["configured"]]
        print(f"  provider    : auto -> {ready[0] if ready else '(none ready)'}"
              f"   candidates: {', '.join(settings.llm_chain[:3])}…")
    else:
        print(f"  provider    : {settings.llm_provider} (manual)")
    print(f"  model chain : {' -> '.join(settings.llm_chain)}")
    if settings.unused_llm_overrides:
        print(f"  note        : {', '.join(settings.unused_llm_overrides)} ignored in auto mode")
    print(f"  search chain: {' -> '.join(settings.search_chain)}")
    configured = [p["name"] for p in describe_providers(settings) if p["configured"]]
    print(f"  providers ready: {', '.join(configured) or 'none'}")
    if not os.path.exists(".env"):
        print("  note: no .env file — copy .env.example to .env unless you set env vars another way")

    results: dict = {
        "model": skipped("not requested (--search-only)"),
        "search": [],
        "agent": skipped("not requested; pass --agent for a full model+search round trip"),
    }
    model_ok = True
    if not args.search_only:
        model_ok, model_reports = asyncio.run(check_model(settings))
        results["model"] = model_reports
    else:
        print("\n1) core model\n  [SKIP] skipped (--search-only)")
    search_ok, search_reports = asyncio.run(check_search(settings, args.query))
    results["search"] = search_reports

    agent_ok = "skipped"
    if args.agent:
        agent_ok, agent_data = asyncio.run(
            check_agent(settings, args.question or "What is the latest Python release? Cite sources.")
        )
        results["agent"] = agent_data

    print("\n" + "=" * 72)
    # An empty chain means the model check cannot pass, even under --search-only.
    model_ok = model_ok if not args.search_only else (bool(build_chain(settings)) or "skipped")
    summary = {
        "model_ok": model_ok,
        "search_ok": search_ok,
        "agent_ok": agent_ok,
    }
    print(f"summary: {json.dumps(summary)}")
    if agent_ok == "skipped":
        print("  agent: skipped — no agent run was requested (use --agent). "
              "In artifacts/doctor.json it is {\"skipped\": true, \"reason\": …}, "
              "not a failure")

    if not search_ok:
        print("Search failed everywhere. Keyless backends: ddg, wikipedia. "
              "Set SEARCH_PROVIDERS=wikipedia,ddg to try the reliable one first.")
    if not args.search_only and not model_ok:
        print("Model failed. .env needs a provider key AND its model, e.g.")
        print("    GROQ_API_KEY=gsk_…   GROQ_MODEL=openai/gpt-oss-120b")
        print("    (model ids: python -m scripts.check_providers --provider groq --models)")

    log.info("doctor finished model_ok=%s search_ok=%s agent_ok=%s", model_ok, search_ok, agent_ok)
    if not model_ok:
        log.warning("doctor: model check failed — see logs/error.log and `python -m scripts.logs errors`")
    if not search_ok:
        log.warning("doctor: search check failed")
    print(f"  logs: {logs.log_dir()}  (python -m scripts.logs errors)")

    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "doctor.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(results, indent=2))

    ok = search_ok and (args.search_only or model_ok) and (agent_ok is not False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
