"""
backend.search.factory
======================

Chooses the web-search backend from ``.env``:

* ``simulated`` — offline deterministic provider (default, always works)
* ``tavily``    — real Tavily API   (requires ``TAVILY_API_KEY``)
* ``serper``    — real Serper.dev   (requires ``SERPER_API_KEY``)

Real providers degrade gracefully to the simulator when their key is
missing, so the pipeline never breaks mid-plan.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

import httpx

from ..config import Settings, settings
from .base import SearchProvider, SearchResponse, SearchResult
from .simulated import SimulatedSearchProvider


class TavilySearchProvider(SearchProvider):
    """Tavily search API adapter (https://tavily.com)."""

    name = "tavily"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.tavily_api_key

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self._key,
                    "query": query,
                    "max_results": max_results,
                },
            )
            response.raise_for_status()
            payload = response.json()

        results: List[SearchResult] = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                source="tavily",
                score=float(item.get("score", 0.0)),
            )
            for item in payload.get("results", [])
        ]
        return SearchResponse(query=query, provider=self.name, results=results, simulated=False)


class SerperSearchProvider(SearchProvider):
    """Serper.dev Google-results adapter (https://serper.dev)."""

    name = "serper"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.serper_api_key

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self._key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
            )
            response.raise_for_status()
            payload = response.json()

        results: List[SearchResult] = [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                source="serper",
                score=1.0 - idx * 0.05,
            )
            for idx, item in enumerate(payload.get("organic", [])[:max_results])
        ]
        return SearchResponse(query=query, provider=self.name, results=results, simulated=False)


@lru_cache(maxsize=1)
def get_search_provider() -> SearchProvider:
    """Return the configured provider (falling back to the simulator)."""
    provider = settings.web_search_provider

    if provider == "tavily" and settings.tavily_api_key:
        return TavilySearchProvider(settings)
    if provider == "serper" and settings.serper_api_key:
        return SerperSearchProvider(settings)
    # Default / missing key → offline simulator keeps the pipeline alive.
    return SimulatedSearchProvider()


def reset_search_cache() -> None:
    """Testing helper: drop the cached provider."""
    get_search_provider.cache_clear()
