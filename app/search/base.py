"""Search result types shared by every backend."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class SearchError(RuntimeError):
    """A search backend failed (network, quota, bot challenge, bad key, ...)."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status: int | None = None,
        hint: str | None = None,
        body: str | None = None,
    ) -> None:
        self.provider = provider
        self.message = message
        self.status = status
        self.hint = hint
        self.body = body
        super().__init__(f"[{provider}] {message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "message": str(self),
            "hint": self.hint,
            "body": (self.body or "")[:400],
        }


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""  # which backend produced it
    published: str | None = None
    rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_tool_output(self, max_chars: int = 600) -> dict[str, Any]:
        """Compact shape handed to the model."""
        snippet = (self.snippet or "").strip()
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars] + "…"
        return {"title": self.title, "url": self.url, "snippet": snippet}


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "results": [r.to_dict() for r in self.results],
            "count": len(self.results),
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
        }
