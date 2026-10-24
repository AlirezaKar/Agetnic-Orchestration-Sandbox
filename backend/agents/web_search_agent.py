"""
backend.agents.web_search_agent
===============================

**Web-search agent** — the open-source search stack in agent form.

Flow
----
1. **Search** — query the configured provider (SearXNG metasearch, or the
   offline simulator in the sandbox).
2. **Deep-crawl** — fetch the top results' full content with **Crawl4AI**
   (LLM-friendly markdown), when a page fetcher is configured.  Skipped
   automatically for simulated results (crawling fake URLs is pointless).
3. **Synthesise** — the LLM answers strictly grounded in the snippets +
   crawled page content, with inline [n] citations.

Everything — query, results, crawled pages, fetcher used — lands in the
run trace's ``data`` payload for evaluation.
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from ..config import settings
from ..llm import BaseLLMClient
from ..schemas import AgentName, AgentResult
from ..search import PageContent, PageFetcher, SearchProvider
from .base import AgentContext, BaseAgent

SYSTEM_PROMPT = """You are the Web Search Agent of a multi-agent orchestration system.

You receive a task, a list of web search results (title / url / snippet) and,
when available, the full crawled content of the top pages. Write a concise,
factual answer **grounded strictly in that material**.

Rules:
- Prefer facts from the crawled page content over bare snippets; cite
  sources inline as [1], [2]… matching the numbered search results.
- If the results are simulated (sandbox/offline mode), say so briefly at
  the end so evaluators know live web data was not used.
- If the material doesn't cover the question, say what is missing instead
  of inventing facts.

Answer now."""


class WebSearchAgent(BaseAgent):
    name = AgentName.WEB_SEARCH
    description = "SearXNG web search + Crawl4AI deep-crawl + grounded summarisation"

    def __init__(
        self,
        llm: BaseLLMClient,
        search_provider: SearchProvider,
        page_fetcher: Optional[PageFetcher] = None,
    ) -> None:
        super().__init__(llm)
        self._search = search_provider
        self._fetcher = page_fetcher  # None → snippets only (deep crawl off)

    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        # ── 1. Search (SearXNG or simulated) ───────────────────
        query = _query_from_task(task)
        search_response = await self._search.search(query, max_results=5)

        # ── 2. Deep-crawl the top results (Crawl4AI) ───────────
        crawled: List[PageContent] = await self._deep_crawl(search_response)

        # ── 3. Grounded synthesis ──────────────────────────────
        numbered = "\n".join(
            f"{i}. {r.title} — {r.url}\n   {r.snippet}"
            for i, r in enumerate(search_response.results, start=1)
        )
        parts = [
            f"## SEARCH QUERY\n{query}",
            f"## SEARCH RESULTS\n{numbered or '(no results returned)'}",
        ]
        if crawled:
            pages = "\n\n".join(
                f"[page {i} · {p.title or 'untitled'} · {p.url}]\n{p.markdown}"
                for i, p in enumerate(crawled, start=1)
            )
            parts.append(f"## PAGE CONTENT\n{pages}")

        answer = await self._llm.complete_text(SYSTEM_PROMPT, "\n\n".join(parts))

        fetcher_used = self._fetcher.name if self._fetcher else "none (snippets only)"
        return AgentResult(
            agent=self.name,
            success=True,
            output=answer,
            thinking=(
                f"Searched with provider '{search_response.provider}' "
                f"({'simulated' if search_response.simulated else 'live'}), "
                f"deep-crawled {len(crawled)} page(s) via '{fetcher_used}', "
                f"and synthesised an answer grounded in "
                f"{len(search_response.results)} result(s)."
            ),
            data={
                "query": query,
                "search": search_response.to_dict(),
                "page_fetcher": fetcher_used,
                "crawled_pages": [p.to_dict() for p in crawled],
            },
        )

    # ── deep crawl ──────────────────────────────────────────────
    async def _deep_crawl(self, search_response) -> List[PageContent]:
        """
        Fetch full content for the top results.

        * Skipped entirely when no fetcher is configured, when
          ``CRAWL4AI_MAX_PAGES`` is 0, or when results are simulated
          (fake URLs).
        * Individual page failures are tolerated — a crawl error becomes
          ``PageContent(ok=False)`` and is simply left out of the prompt.
        """
        if self._fetcher is None or settings.crawl4ai_max_pages <= 0:
            return []
        if getattr(search_response, "simulated", False):
            return []

        targets = [r.url for r in search_response.results[: settings.crawl4ai_max_pages]]
        results = await asyncio.gather(
            *(self._fetch_one(url) for url in targets),
            return_exceptions=False,
        )
        # Only successful pages with content reach the LLM prompt.
        return [p for p in results if p.ok and p.markdown.strip()]

    async def _fetch_one(self, url: str) -> PageContent:
        """Fetch one page with a hard time bound; never raises."""
        try:
            return await asyncio.wait_for(
                self._fetcher.fetch(url),
                timeout=settings.page_fetch_timeout + 15.0,
            )
        except asyncio.TimeoutError:
            return PageContent(url=url, ok=False, error="page fetch timed out")
        except Exception as exc:  # noqa: BLE001 — never break the task
            return PageContent(url=url, ok=False, error=f"{type(exc).__name__}: {exc}")


def _query_from_task(task: str) -> str:
    """
    Turn a Planner instruction into a clean search query.

    The Planner writes instructions like
    ``Search the web for up-to-date information relevant to: "…"`` —
    we extract the quoted core when present, otherwise strip common
    imperative prefixes.
    """
    quoted = re.search(r'"([^"]+)"', task)
    if quoted:
        return quoted.group(1).strip()
    cleaned = re.sub(
        r"^(search the web for|search for|look up|find|research)\b",
        "",
        task,
        flags=re.I,
    ).strip(" :.-")
    return cleaned or task
