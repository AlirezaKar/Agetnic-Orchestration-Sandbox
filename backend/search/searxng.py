"""
backend.search.searxng
======================

**SearXNG** search provider (https://github.com/searxng/searxng).

SearXNG is a free, self-hosted metasearch engine that aggregates results
from many upstream engines (Google, Bing, DuckDuckGo, …) without tracking.
It exposes a JSON API which must be enabled on the instance::

    # settings.yml of the SearXNG instance
    search:
      formats:
        - html
        - json

Request shape  →  ``GET {base_url}/search?q=…&format=json``
Parameters     →  ``q``, ``categories``, ``language``, ``pageno``,
                  ``time_range`` (day|month|year), ``safesearch`` (0|1|2)

Run one locally (docker)::

    docker run -d -p 8888:8080 \
      -v ./searxng-settings.yml:/etc/searxng/settings.yml \
      searxng/searxng

Instances may sit behind auth — set ``SEARXNG_API_KEY`` and the key is
sent both as ``Authorization: Bearer …`` and ``X-API-Key``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings
from .base import SearchError, SearchProvider, SearchResponse, SearchResult


def parse_searxng_payload(
    payload: Dict[str, Any],
    query: str,
    max_results: int,
    *,
    provider: str = "searxng",
) -> SearchResponse:
    """
    Convert a SearXNG JSON body into a :class:`SearchResponse`.

    Kept as a pure function so it can be unit-tested without any network.
    Relevant per-result fields in the SearXNG payload:
    ``title``, ``url``, ``content`` (snippet), ``engines`` (list of names),
    ``score``, ``publishedDate``.
    """
    results: List[SearchResult] = []
    for item in payload.get("results", [])[:max_results]:
        engines = item.get("engines") or []
        results.append(
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "") or ""),
                source=provider,
                engine=", ".join(engines) if isinstance(engines, list) else str(engines),
                score=float(item.get("score", 0.0) or 0.0),
            )
        )
    return SearchResponse(
        query=query,
        provider=provider,
        results=results,
        simulated=False,
    )


class SearXNGSearchProvider(SearchProvider):
    """Talks to a SearXNG instance's JSON API."""

    name = "searxng"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._settings.searxng_api_key:
            headers["Authorization"] = f"Bearer {self._settings.searxng_api_key}"
            headers["X-API-Key"] = self._settings.searxng_api_key
        return headers

    def _params(self, query: str, max_results: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "q": query,
            "format": "json",
            "safesearch": self._settings.searxng_safesearch,
            "pageno": 1,
        }
        if self._settings.searxng_language:
            params["language"] = self._settings.searxng_language
        if self._settings.searxng_time_range:
            params["time_range"] = self._settings.searxng_time_range
        return params

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        base = self._settings.searxng_base_url.rstrip("/")
        url = f"{base}/search"

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(
                    url,
                    params=self._params(query, max_results),
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise SearchError(f"SearXNG request failed: {exc}") from exc

        if response.status_code == 403:
            # SearXNG answers 403 when `format=json` is not enabled on the
            # instance — a very common first-run stumbling block.
            raise SearchError(
                "SearXNG returned 403: enable the JSON output format on the "
                "instance (search.formats: [html, json] in settings.yml)."
            )
        if response.status_code >= 400:
            raise SearchError(f"SearXNG error {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchError(f"SearXNG returned non-JSON content: {exc}") from exc

        return parse_searxng_payload(payload, query, max_results, provider=self.name)
