"""
backend.search.base
===================

Provider-agnostic web-search contract.  The Web-search agent only ever
talks to :class:`SearchProvider`, so swapping the offline simulator for a
real API (Tavily, Serper, ...) is a pure configuration change.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List


@dataclass
class SearchResult:
    """One search hit, close to what real search APIs return."""

    title: str
    url: str
    snippet: str
    source: str = "unknown"
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "score": self.score,
        }


@dataclass
class SearchResponse:
    """Provider answer for one query (kept simple)."""

    query: str
    provider: str
    results: List[SearchResult] = field(default_factory=list)
    simulated: bool = False

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "provider": self.provider,
            "simulated": self.simulated,
            "results": [r.to_dict() for r in self.results],
        }


class SearchProvider(abc.ABC):
    """Interface every web-search backend must implement."""

    name: str = "base"

    @abc.abstractmethod
    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        """Return ranked results for ``query``."""
