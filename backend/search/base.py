"""
backend.search.base
===================

Provider-agnostic contracts for the open-source search stack:

* **SearchProvider** — query → ranked results.
  Implementations: ``simulated`` (offline) and ``searxng`` (SearXNG
  metasearch engine).
* **PageFetcher** — url → LLM-friendly page content (markdown).
  Implementations: ``crawl4ai`` (headless-browser crawler) and
  ``simple_http`` (lightweight httpx fallback).

The Web-search agent combines both: SearXNG finds candidate pages,
Crawl4AI deep-crawls the best ones, the LLM answers from the content.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional


class SearchError(RuntimeError):
    """Raised when a search provider or page fetcher fails hard."""


@dataclass
class SearchResult:
    """One search hit, close to what a metasearch API returns."""

    title: str
    url: str
    snippet: str
    source: str = "unknown"      # provider name (simulated / searxng)
    engine: str = ""             # upstream engine(s) that produced the hit
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "engine": self.engine,
            "score": self.score,
        }


@dataclass
class SearchResponse:
    """Provider answer for one query (kept simple)."""

    query: str
    provider: str
    results: List[SearchResult] = field(default_factory=list)
    simulated: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "provider": self.provider,
            "simulated": self.simulated,
            "error": self.error,
            "results": [r.to_dict() for r in self.results],
        }


class SearchProvider(abc.ABC):
    """Interface every search backend must implement."""

    name: str = "base"

    @abc.abstractmethod
    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        """Return ranked results for ``query``."""


# ──────────────────────────────────────────────────────────────────
#  Page fetching (deep crawl)
# ──────────────────────────────────────────────────────────────────
@dataclass
class PageContent:
    """Extracted content of one crawled web page."""

    url: str
    ok: bool = True
    title: str = ""
    markdown: str = ""           # LLM-friendly representation of the page
    fetcher: str = "unknown"     # crawl4ai / simple_http
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "ok": self.ok,
            "title": self.title,
            "fetcher": self.fetcher,
            "markdown_chars": len(self.markdown),
            "markdown": self.markdown,
            "error": self.error,
        }


class PageFetcher(abc.ABC):
    """Interface for turning a URL into LLM-consumable content."""

    name: str = "base"

    @abc.abstractmethod
    async def fetch(self, url: str) -> PageContent:
        """Fetch one page and extract its content. Never raises: failures
        come back as ``PageContent(ok=False, error=…)``."""
