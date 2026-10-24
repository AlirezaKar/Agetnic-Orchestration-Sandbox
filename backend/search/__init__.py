"""
backend.search — the open-source web-search stack.

``base``              contracts: SearchProvider + PageFetcher + types
``simulated``         deterministic offline provider (sandbox/testing default)
``searxng``           SearXNG metasearch engine (self-hosted, free)
``crawl4ai_fetcher``  Crawl4AI deep-crawl → LLM-friendly markdown
``simple_fetcher``    lightweight httpx fallback fetcher
``factory``           provider selection from .env
"""

from .base import (
    PageContent,
    PageFetcher,
    SearchError,
    SearchProvider,
    SearchResponse,
    SearchResult,
)
from .factory import (
    get_page_fetcher,
    get_search_provider,
    reset_search_cache,
    shutdown_search,
)

__all__ = [
    "PageContent",
    "PageFetcher",
    "SearchError",
    "SearchProvider",
    "SearchResponse",
    "SearchResult",
    "get_page_fetcher",
    "get_search_provider",
    "reset_search_cache",
    "shutdown_search",
]
