"""Build the ordered search chain and merge its failures into one report."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.search.base import SearchError, SearchResponse, SearchResult
from app.search.presets import SEARCH_PRESETS
from app.search.providers import PROVIDER_CLASSES, BaseSearchProvider


def _normalise_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower().replace("www.", "")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def dedupe_results(results: list[SearchResult], limit: int) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for result in results:
        key = _normalise_url(result.url)
        if not key or key in seen:
            continue
        seen.add(key)
        result.rank = len(out) + 1
        out.append(result)
        if len(out) >= limit:
            break
    return out


class SearchChain:
    """Tries each configured backend in order; the first non-empty result wins."""

    def __init__(
        self,
        providers: list[BaseSearchProvider],
        settings: Settings,
        skipped: list[str] | None = None,
    ):
        self.providers = providers
        self.settings = settings
        self.skipped = skipped or []

    def __bool__(self) -> bool:
        return bool(self.providers)

    @property
    def primary(self) -> BaseSearchProvider:
        return self.providers[0]

    async def search(self, query: str, max_results: int | None = None) -> SearchResponse:
        limit = max_results or self.settings.search_max_results
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        if not self.providers:
            raise SearchError(
                "config",
                "No usable search provider. Set SEARCH_PROVIDER (ddg needs no key) "
                "in .env — see .env.example.",
            )
        for provider in self.providers:
            try:
                results = await provider.search(query, limit)
            except SearchError as exc:
                attempts.append({**exc.to_dict(), "ok": False, "empty": True})
                continue
            attempts.append(
                {
                    "provider": provider.name,
                    "ok": bool(results),
                    "count": len(results),
                    "empty": not results,
                }
            )
            if results:
                final = dedupe_results(results, limit) if self.settings.search_dedupe else results[:limit]
                return SearchResponse(
                    query=query,
                    results=final,
                    provider=provider.name,
                    attempts=attempts,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )

        # Every backend came back empty. Distinguish "all raised" from "no hits".
        if all("message" in attempt for attempt in attempts):
            detail = "; ".join(f"{a['provider']}: {a['message']}" for a in attempts)
            raise SearchError("chain", f"all search backends failed ({detail})", body=str(attempts))
        return SearchResponse(
            query=query,
            results=[],
            provider=attempts[-1]["provider"] if attempts else "",
            attempts=attempts,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def build_search_chain(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SearchChain:
    providers: list[BaseSearchProvider] = []
    skipped: list[str] = []
    for name in settings.search_chain:
        key = (name or "").strip().lower()
        if key not in PROVIDER_CLASSES:
            skipped.append(
                f"unknown search provider '{name}' "
                f"(known: {', '.join(sorted(PROVIDER_CLASSES))})"
            )
            continue
        preset = SEARCH_PRESETS.get(key)
        if preset and preset.needs_key:
            missing = [env for env in preset.key_env if not getattr(settings, env.lower(), None)]
            if missing:
                skipped.append(f"{key}: missing {', '.join(missing)}")
                continue
        providers.append(PROVIDER_CLASSES[key](settings, transport=transport))
    return SearchChain(providers, settings, skipped)


def describe_search_providers(settings: Settings) -> list[dict[str, Any]]:
    active = set(settings.search_chain)
    out: list[dict[str, Any]] = []
    for name, preset in SEARCH_PRESETS.items():
        configured = all(getattr(settings, env.lower(), None) for env in preset.key_env)
        out.append(
            {
                "name": name,
                "label": preset.label,
                "configured": configured,
                "status": "ready" if configured else "missing_key",
                "needs_key": preset.needs_key,
                "needs_server": preset.needs_server,
                "api_key_envs": list(preset.key_env),
                "key_url": preset.key_url,
                "docs_url": preset.docs_url,
                "free_tier": preset.free_tier,
                "notes": preset.notes,
                "is_active": name in active,
            }
        )
    return out
