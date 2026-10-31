#!/usr/bin/env python3
"""Verify model providers: what is configured, does it answer, what models exist.

Examples
--------
    python -m scripts.check_providers                 # table of all providers
    python -m scripts.check_providers --ping          # ping the configured chain
    python -m scripts.check_providers --models        # live model IDs (needs a key)
    python -m scripts.check_providers --provider gemini --models
    python -m scripts.check_providers --provider gemini --models --autofix
    python -m scripts.check_providers --links         # where to get keys/models

Exit code 0 if every pinged provider answered, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.llm.base import ProviderError
from app.llm.presets import PRESETS
from app.llm.registry import (
    ConfigurationError,
    build_chain,
    build_provider,
    describe_providers,
    resolve_named,
)

ARTIFACTS = Path("artifacts")


def print_table(settings) -> None:
    providers = describe_providers(settings)
    chain = settings.llm_chain
    print(f"\ncore provider: {settings.llm_provider}   chain: {' -> '.join(chain)}\n" + "-" * 96)
    print(f"{'provider':<14}{'status':<13}{'free tier':<48}model")
    print("-" * 96)
    for p in providers:
        status = {
            "ready": "ready",
            "missing_model": "no model",
            "incomplete": "incomplete",
        }.get(p["status"], "no key")
        if p["name"] in chain:
            status += " *"
        free = (p["free_tier"][:46] + "…") if len(p["free_tier"]) > 47 else p["free_tier"]
        print(f"{p['name']:<14}{status:<13}{free:<48}{p['model_value']}")
    print("-" * 96)
    print("* = active in LLM_PROVIDER / LLM_FALLBACKS")
    missing = [p for p in providers if not p["configured"] and p["name"] in chain]
    for p in missing:
        print(f"  {p['name']}: set {', '.join(p['missing_env'])}  -> key: {p['key_url']}")


def print_links(settings) -> None:
    print("\nprovider     get a key                              model list")
    print("-" * 110)
    for p in describe_providers(settings):
        key = (p["key_url"][:40] + "…") if len(p["key_url"]) > 41 else p["key_url"]
        print(f"{p['name']:<13}{key:<42}{p['models_url']}")


async def ping_chain(settings, as_json: bool) -> int:
    chain = build_chain(settings)
    if chain.skipped:
        for note in chain.skipped:
            print(f"skip: {note}")
    if not chain:
        print("No provider is configured. Set LLM_PROVIDER + its API key in .env "
              "(run with --links to see where).")
        return 1

    reports = [await provider.ping() for provider in chain.providers]
    if as_json:
        print(json.dumps(reports, indent=2))
    else:
        print("\nmodel ping\n" + "-" * 72)
        for report in reports:
            if report["ok"]:
                print(f"  PASS   {report['provider']:<12} {report['model']}  "
                      f"{report['latency_ms']} ms  reply={report['reply']!r}")
            else:
                error = report["error"]
                print(f"  FAIL   {report['provider']:<12} {error['message']}")
                if error.get("hint"):
                    print(f"         hint: {error['hint']}")
        print("-" * 72)

    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "provider-ping.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0 if all(r["ok"] for r in reports) else 1


def _write_env_model(model_field: str, value: str) -> str:
    """Point ``<PROVIDER>_MODEL`` at ``value`` inside .env (with a backup)."""
    env_path = Path(".env")
    key = model_field.upper()
    line = f"{key}={value}"
    if not env_path.exists():
        env_path.write_text(
            "# created by scripts.check_providers --autofix\n" + line + "\n",
            encoding="utf-8",
        )
        return f"created .env with {line}"
    original = env_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    replaced = False
    for index, existing in enumerate(lines):
        if existing.split("=", 1)[0].strip() == key:
            lines[index] = line
            replaced = True
            break
    if not replaced:
        lines.append(line)
    (env_path.parent / (env_path.name + ".bak")).write_text(original, encoding="utf-8")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"updated {key} in .env (backup: .env.bak)"


async def list_models(
    settings, provider_name: str | None, as_json: bool, autofix: bool = False
) -> int:
    chain = build_chain(settings)
    if provider_name:
        targets = [provider_name.lower()]
    elif chain:
        targets = [p.name for p in chain.providers]
    else:
        targets = [p["name"] for p in describe_providers(settings) if p["configured"]]
    if not targets:
        print("Nothing to list: no provider configured. Try --provider groq with GROQ_API_KEY set.")
        return 1

    exit_code = 0
    for name in targets:
        try:
            provider = build_provider(resolve_named(name, settings), settings)
            models = await provider.list_models()
            recommended = (await provider.ranked_models())[:5]
        except (ConfigurationError, ProviderError) as exc:
            exit_code = 1
            print(f"\n{name}: {exc}")
            if getattr(exc, "hint", None):
                print(f"  hint: {exc.hint}")
            continue
        if as_json:
            print(json.dumps({"provider": name, "recommended": recommended, "models": models}, indent=2))
            continue
        print(f"\n{name} — {len(models)} models at {provider.base_url}")
        if recommended:
            print("  recommended (auto order):")
            for model in recommended:
                print(f"    * {model}")
            print("  all models:")
        for model in models:
            print(f"    {model}")
        if not models:
            print("  (provider returned an empty list)")
        if autofix:
            preset = PRESETS.get(name)
            best = recommended[0] if recommended else None
            if preset and best:
                print("  " + _write_env_model(preset.model_field, best))
            else:
                print("  nothing to write: no usable model found")

    ARTIFACTS.mkdir(exist_ok=True)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and test model providers.")
    parser.add_argument("--provider", help="target a single provider")
    parser.add_argument("--ping", action="store_true", help="send a tiny completion to each configured provider")
    parser.add_argument("--models", action="store_true", help="list model IDs from the provider API")
    parser.add_argument(
        "--autofix",
        action="store_true",
        help="with --models: write the recommended live model id into .env",
    )
    parser.add_argument("--links", action="store_true", help="print key/model-list URLs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if args.links:
        print_links(settings)
        return 0
    if args.models:
        return asyncio.run(list_models(settings, args.provider, args.json, args.autofix))
    if args.ping:
        return asyncio.run(ping_chain(settings, args.json))

    print_table(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
