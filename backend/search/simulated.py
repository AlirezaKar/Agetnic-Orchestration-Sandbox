"""
backend.search.simulated
========================

Offline web-search provider.

Why it exists
-------------
The sandbox must be able to exercise the full orchestration pipeline on
machines with no internet.  This provider fabricates *plausible, clearly
labelled* search results deterministically from the query text — the same
shape a real API returns — so the Web-search agent and the Manager behave
exactly as they would with live data.

Results are marked ``simulated: true`` everywhere (API payload + traces)
so nobody can mistake them for real web content.
"""

from __future__ import annotations

import hashlib

from .base import SearchProvider, SearchResponse, SearchResult

# Small built-in knowledge nuggets.  Matched by keyword, they make the
# simulated results look relevant to common demo queries.
_KNOWLEDGE = [
    (["llm", "language model", "groq", "ai"], "Large Language Models (LLMs) are neural networks trained on text corpora; providers such as Groq offer fast inference with free tiers."),
    (["orchestr", "agent", "multi-agent"], "Multi-agent orchestration routes each sub-task to a specialist agent; a manager coordinates planning, parallel execution and result synthesis."),
    (["rag", "retrieval", "document"], "Retrieval-Augmented Generation (RAG) grounds model answers in retrieved document passages to reduce hallucination."),
    (["search", "web"], "Web search stacks (SearXNG metasearch, Bing, DuckDuckGo) return ranked title/url/snippet triples that agents crawl and summarise into grounded answers."),
    (["data", "analy", "statistic", "number"], "Data analysis pipelines clean, aggregate and visualise quantitative data; statistics summarise central tendency and spread."),
    (["weather", "climate"], "Weather forecasts combine atmospheric models with observations; APIs expose current conditions and multi-day outlooks."),
    (["market", "competitor", "business"], "Market analysis studies competitors, pricing and trends to inform strategy; reports mix qualitative and quantitative findings."),
    (["python", "code", "software"], "Software engineering practices emphasise modular design, tests and observability; Python is widely used for AI orchestration backends."),
]


def _pseudo_rank(query: str) -> int:
    """Stable pseudo-random seed derived from the query."""
    return int(hashlib.sha256(query.lower().encode()).hexdigest()[:8], 16)


class SimulatedSearchProvider(SearchProvider):
    """Deterministic fake search engine (default provider)."""

    name = "simulated"

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        seed = _pseudo_rank(query)
        lower = query.lower()

        # Pick knowledge nuggets relevant to the query.
        matched = [text for keywords, text in _KNOWLEDGE if any(k in lower for k in keywords)]
        if not matched:
            matched = [
                f"Overview of '{query}': background, definitions and current perspectives.",
                f"Recent discussions about '{query}' highlight practical use-cases and open challenges.",
                f"Guides and tutorials related to '{query}' with step-by-step explanations.",
            ]

        results: list[SearchResult] = []
        slug = "-".join(lower.split()[:6]) or "topic"
        for i in range(min(max_results, max(len(matched), 3))):
            snippet = matched[i % len(matched)]
            rank_shift = (seed >> (i * 3)) % 5  # deterministic per-result jitter
            results.append(
                SearchResult(
                    title=f"{query.title()} — reference {i + 1}",
                    url=f"https://simulated.example/{slug}/{i + 1}?r={rank_shift}",
                    snippet=snippet,
                    source="simulated",
                    score=round(1.0 - i * 0.12 - rank_shift * 0.01, 3),
                )
            )

        return SearchResponse(query=query, provider=self.name, results=results, simulated=True)
