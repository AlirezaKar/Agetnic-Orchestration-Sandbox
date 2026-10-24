"""
backend.search.factory
======================

Wires the open-source search stack from ``.env``:

Search (query → results)
    ``WEB_SEARCH_PROVIDER=simulated`` → deterministic offline provider
    ``WEB_SEARCH_PROVIDER=searxng``   → SearXNG metasearch JSON API

Deep crawl (url → markdown content)
    ``CRAWL4AI_ENABLED=true``  → **Crawl4AI** when the package is installed,
    otherwise a lightweight ``simple_http`` fetcher so the pipeline keeps
    working.  ``CRAWL4AI_ENABLED=false`` turns deep crawling off entirely
    (the agent then works from search snippets only).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from ..config import Settings, settings
from .base import PageFetcher, SearchProvider
from .crawl4ai_fetcher import Crawl4AIFetcher
from .searxng import SearXNGSearchProvider
from .simple_fetcher import SimpleHttpFetcher
from .simulated import SimulatedSearchProvider


@lru_cache(maxsize=1)
def get_search_provider() -> SearchProvider:
    """Return the configured search backend (offline simulator as default)."""
    provider = settings.web_search_provider

    if provider == "searxng":
        return SearXNGSearchProvider(settings)
    # Default / unknown value → offline simulator keeps the pipeline alive.
    return SimulatedSearchProvider()


@lru_cache(maxsize=1)
def get_page_fetcher() -> Optional[PageFetcher]:
    """
    Return the configured page fetcher.

    Priority: Crawl4AI (if enabled *and* installed) → simple HTTP fetcher
    (if deep crawling is enabled) → ``None`` (deep crawling disabled).
    """
    if not settings.crawl4ai_enabled:
        return None
    if Crawl4AIFetcher.is_available():
        return Crawl4AIFetcher(settings)
    return SimpleHttpFetcher(settings)


def reset_search_cache() -> None:
    """Drop cached search providers/fetchers (sync helper for tests)."""
    get_page_fetcher.cache_clear()
    get_search_provider.cache_clear()


async def shutdown_search() -> None:
    """Close any live browser-backed page fetcher and clear search caches."""
    if get_page_fetcher.cache_info().currsize > 0:
        fetcher = get_page_fetcher()
        close_fn = getattr(fetcher, "close", None)
        if callable(close_fn):
            await close_fn()
    reset_search_cache()
