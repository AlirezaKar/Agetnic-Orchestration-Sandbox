"""
backend.search.crawl4ai_fetcher
===============================

**Crawl4AI** page fetcher (https://github.com/unclecode/crawl4ai).

Crawl4AI is an open-source, LLM-friendly web crawler: it renders pages in
a headless browser (Playwright) and returns clean markdown — exactly the
representation an LLM can reason over.

Integration notes
-----------------
* ``crawl4ai`` is imported **lazily** and is optional: when the package
  (or its browser binaries) is missing, the factory falls back to
  :class:`~backend.search.simple_fetcher.SimpleHttpFetcher`.
* The underlying ``AsyncWebCrawler`` is created once and reused — browser
  startup dominates crawl time.  ``reset_search_cache`` drops it.
* Failures never propagate: a failed page comes back as
  ``PageContent(ok=False, error=…)`` so one dead link can't kill a task.

Setup on a real machine::

    pip install crawl4ai
    crawl4ai-setup          # downloads the Playwright browser once
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..config import Settings
from .base import PageContent, PageFetcher


class Crawl4AIFetcher(PageFetcher):
    """Fetch pages via Crawl4AI's headless-browser crawler."""

    name = "crawl4ai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._crawler: Optional[Any] = None      # cached AsyncWebCrawler
        self._crawler_lock = asyncio.Lock()

    # ── availability ────────────────────────────────────────────
    @staticmethod
    def is_available() -> bool:
        """True when the crawl4ai package can be imported."""
        try:
            import crawl4ai  # noqa: F401
            return True
        except ImportError:
            return False

    # ── crawler lifecycle (created once, reused) ────────────────
    async def _get_crawler(self):
        async with self._crawler_lock:
            if self._crawler is not None:
                return self._crawler

            from crawl4ai import AsyncWebCrawler, BrowserConfig

            browser_config = BrowserConfig(
                headless=True,
                verbose=False,
            )
            crawler = AsyncWebCrawler(config=browser_config)
            # AsyncWebCrawler is an async context manager; start it once and
            # keep the instance alive for the process lifetime.
            await crawler.__aenter__()
            self._crawler = crawler
            return crawler

    async def close(self) -> None:
        """Release the browser (called on shutdown / cache reset)."""
        async with self._crawler_lock:
            if self._crawler is not None:
                try:
                    await self._crawler.__aexit__(None, None, None)
                finally:
                    self._crawler = None

    # ── fetching ────────────────────────────────────────────────
    async def fetch(self, url: str) -> PageContent:
        try:
            from crawl4ai import CacheMode, CrawlerRunConfig

            # Browser startup is bounded too — a stuck launch can't hang
            # the whole agent task.
            crawler = await asyncio.wait_for(
                self._get_crawler(),
                timeout=self._settings.page_fetch_timeout + 15.0,
            )
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.ENABLED,
                page_timeout=int(self._settings.page_fetch_timeout * 1000),
            )
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=run_config),
                timeout=self._settings.page_fetch_timeout + 15.0,
            )
        except asyncio.TimeoutError:
            return PageContent(url=url, ok=False, fetcher=self.name,
                               error=f"crawl timed out after {self._settings.page_fetch_timeout:.0f}s")
        except Exception as exc:  # noqa: BLE001 — crawl errors are data, not crashes
            return PageContent(url=url, ok=False, fetcher=self.name,
                               error=f"{type(exc).__name__}: {exc}")

        if not getattr(result, "success", True):
            return PageContent(
                url=url,
                ok=False,
                fetcher=self.name,
                error=getattr(result, "error_message", None) or "crawl reported failure",
            )

        # `result.markdown` is str-compatible (StringCompatibleMarkdown);
        # older/newer versions may hand back either shape.
        markdown = getattr(result, "markdown", "") or ""
        markdown = str(markdown)
        max_chars = self._settings.crawl4ai_max_content_chars
        if len(markdown) > max_chars:
            markdown = markdown[:max_chars] + "\n\n[…content truncated…]"

        metadata = getattr(result, "metadata", None) or {}
        title = str(metadata.get("title", "") or "")

        return PageContent(
            url=url,
            ok=True,
            title=title,
            markdown=markdown,
            fetcher=self.name,
        )
