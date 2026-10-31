"""Concrete web-search backends.

Each provider exposes the same ``search(query, max_results) -> list[SearchResult]``
coroutine, so the agent never cares which one is configured.
"""

from __future__ import annotations

import html as html_lib
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from app.config import Settings
from app.search.base import SearchError, SearchResult

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
}
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return " ".join(html_lib.unescape(_TAG_RE.sub(" ", text or "")).split())


class BaseSearchProvider:
    name = "base"
    label = "base"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    # ------------------------------------------------------------------ http
    def _client(self, *, accept: str = "application/json") -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.settings.search_timeout,
            transport=self._transport,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.search_user_agent,
                "Accept": accept,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        raise NotImplementedError

    async def ping(self, query: str = "python asyncio tutorial") -> dict[str, Any]:
        """End-to-end self test used by /search/diagnose and scripts/doctor.py."""
        started = time.perf_counter()
        try:
            results = await self.search(query, max_results=3)
        except SearchError as exc:
            return {
                "provider": self.name,
                "ok": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": exc.to_dict(),
            }
        return {
            "provider": self.name,
            "ok": bool(results),
            "count": len(results),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "sample": [r.to_dict() for r in results[:2]],
            "empty": not results,
        }


# --------------------------------------------------------------------- DDG
class _DDGParser(HTMLParser):
    """Extracts title/snippet blocks from DuckDuckGo's no-JS HTML pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._depth = 0
        self._capture: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: (value or "") for key, value in attrs}
        classes = attr.get("class", "")
        kind = None
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            kind = "title"
        elif tag == "a" and "result__snippet" in classes:
            kind = "snippet"
        elif tag == "div" and "result__snippet" in classes:
            kind = "snippet"
        elif tag == "td" and "result-snippet" in classes:
            kind = "snippet"
        elif tag == "a" and "result__url" in classes:
            kind = "url"

        if kind:
            self._flush()
            self._capture = {
                "kind": kind,
                "href": attr.get("href"),
                "text": [],
                "depth": self._depth,
            }
        if tag not in _VOID_TAGS:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        self._depth -= 1
        if self._capture and self._depth <= self._capture["depth"]:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture["text"].append(data)

    def close(self) -> None:  # noqa: D401 - stdlib signature
        super().close()
        self._flush()

    def _flush(self) -> None:
        if not self._capture:
            return
        text = " ".join("".join(self._capture["text"]).split())
        if text:
            self.items.append(
                {"kind": self._capture["kind"], "href": self._capture["href"], "text": text}
            )
        self._capture = None


def _clean_ddg_url(href: str | None) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        params = parse_qs(parsed.query)
        if "uddg" in params:
            return unquote(params["uddg"][0])
    return href


def parse_ddg_html(page: str) -> list[SearchResult]:
    """Pure function so it can be unit tested without touching the network."""
    parser = _DDGParser()
    parser.feed(page)
    parser.close()

    results: list[SearchResult] = []
    for item in parser.items:
        if item["kind"] == "title":
            url = _clean_ddg_url(item["href"])
            if not url or "duckduckgo.com/y.js" in url:  # advertisement
                continue
            results.append(SearchResult(title=item["text"], url=url, source="ddg"))
        elif item["kind"] == "snippet" and results and not results[-1].snippet:
            results[-1].snippet = item["text"]
    return results


class DuckDuckGoSearch(BaseSearchProvider):
    name = "ddg"
    label = "DuckDuckGo (HTML)"
    HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
    LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        payload = {
            "q": query,
            "kl": self.settings.duckduckgo_region,
            "kp": {"off": "-2", "moderate": "-1", "strict": "1"}.get(
                self.settings.duckduckgo_safe_search.lower(), "-1"
            ),
        }
        last_error: SearchError | None = None
        for endpoint in (self.HTML_ENDPOINT, self.LITE_ENDPOINT):
            try:
                async with self._client(accept="text/html") as client:
                    response = await client.post(endpoint, data=payload)
            except httpx.HTTPError as exc:
                last_error = SearchError(
                    self.name,
                    f"network error: {type(exc).__name__}: {exc}",
                    hint="No outbound internet access from this host, or DuckDuckGo is unreachable.",
                )
                continue

            body = response.text
            lowered = body.lower()
            challenged = (
                response.status_code in (202, 403, 418, 429)
                or "anomaly-modal" in lowered
                or "unusual traffic" in lowered
                or ("captcha" in lowered and "result__a" not in lowered)
            )
            results = parse_ddg_html(body)
            if results:
                return results[:max_results]
            last_error = SearchError(
                self.name,
                "empty result page"
                + (" (bot challenge detected)" if challenged else ""),
                status=response.status_code,
                hint=(
                    "DuckDuckGo served a bot-challenge page (common from VPS/datacenter IPs "
                    "or under rapid polling). Add a keyed provider to the chain, e.g. "
                    "SEARCH_PROVIDERS=tavily,ddg, or fall back to wikipedia."
                )
                if challenged
                else "DuckDuckGo markup may have changed; try another provider.",
                body=body[:300],
            )
        raise last_error or SearchError(self.name, "search failed")


# --------------------------------------------------------------- Wikipedia
class WikipediaSearch(BaseSearchProvider):
    name = "wikipedia"
    label = "Wikipedia (MediaWiki API)"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        lang = (self.settings.wikipedia_lang or "en").strip() or "en"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": max(1, min(max_results, 20)),
            "srprop": "snippet",
        }
        try:
            async with self._client() as client:
                response = await client.get(
                    f"https://{lang}.wikipedia.org/w/api.php", params=params
                )
        except httpx.HTTPError as exc:
            raise SearchError(
                self.name, f"network error: {type(exc).__name__}: {exc}"
            ) from exc
        if not response.is_success:
            raise SearchError(
                self.name,
                f"HTTP {response.status_code}",
                status=response.status_code,
                body=response.text[:300],
            )
        data = response.json()
        hits = ((data.get("query") or {}).get("search")) or []
        results = []
        for hit in hits:
            title = hit.get("title", "")
            results.append(
                SearchResult(
                    title=title,
                    url=f"https://{lang}.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}",
                    snippet=_strip_html(hit.get("snippet", "")),
                    source=self.name,
                )
            )
        return results[:max_results]


# ------------------------------------------------------------------ Tavily
class TavilySearch(BaseSearchProvider):
    name = "tavily"
    label = "Tavily"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        key = self.settings.tavily_api_key
        if not key:
            raise SearchError(self.name, "TAVILY_API_KEY is not set", hint="Get one at https://app.tavily.com/home")
        payload = {
            "query": query,
            "max_results": max(1, min(max_results, 20)),
            "search_depth": "basic",
            "include_answer": False,
        }
        try:
            async with self._client() as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise SearchError(self.name, f"network error: {type(exc).__name__}: {exc}") from exc
        self._check(response)
        data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                published=item.get("published_date"),
                source=self.name,
            )
            for item in data.get("results", [])
        ][:max_results]

    def _check(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        hint = None
        if response.status_code in (401, 403):
            hint = "Bad/expired Tavily key. Recreate it at https://app.tavily.com/home (it starts with tvly-)."
        elif response.status_code == 429:
            hint = "Monthly free credits (1,000) exhausted; wait for reset or switch provider."
        raise SearchError(
            self.name,
            f"HTTP {response.status_code}",
            status=response.status_code,
            hint=hint,
            body=response.text[:400],
        )


# ------------------------------------------------------------------ Serper
class SerperSearch(BaseSearchProvider):
    name = "serper"
    label = "Serper (Google)"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        key = self.settings.serper_api_key
        if not key:
            raise SearchError(self.name, "SERPER_API_KEY is not set", hint="Get one at https://serper.dev/api-key")
        try:
            async with self._client() as client:
                response = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": key},
                    json={"q": query, "num": max(1, min(max_results, 20))},
                )
        except httpx.HTTPError as exc:
            raise SearchError(self.name, f"network error: {type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise SearchError(
                self.name,
                f"HTTP {response.status_code}",
                status=response.status_code,
                hint="Check SERPER_API_KEY; free credits may be used up."
                if response.status_code in (401, 403, 429)
                else None,
                body=response.text[:400],
            )
        data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                published=item.get("date"),
                source=self.name,
            )
            for item in data.get("organic", [])
        ][:max_results]


# --------------------------------------------------------------------- Exa
class ExaSearch(BaseSearchProvider):
    name = "exa"
    label = "Exa"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        key = self.settings.exa_api_key
        if not key:
            raise SearchError(self.name, "EXA_API_KEY is not set", hint="Get one at https://dashboard.exa.ai/api-keys")
        try:
            async with self._client() as client:
                response = await client.post(
                    "https://api.exa.ai/search",
                    headers={"x-api-key": key},
                    json={
                        "query": query,
                        "numResults": max(1, min(max_results, 20)),
                        "contents": {"text": {"maxCharacters": 600}},
                    },
                )
        except httpx.HTTPError as exc:
            raise SearchError(self.name, f"network error: {type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise SearchError(
                self.name,
                f"HTTP {response.status_code}",
                status=response.status_code,
                hint="Check EXA_API_KEY / free monthly credit balance."
                if response.status_code in (401, 403, 429)
                else None,
                body=response.text[:400],
            )
        data = response.json()
        return [
            SearchResult(
                title=item.get("title") or item.get("url", ""),
                url=item.get("url", ""),
                snippet=item.get("text", "") or "",
                published=item.get("publishedDate"),
                source=self.name,
            )
            for item in data.get("results", [])
        ][:max_results]


# -------------------------------------------------------------- Google CSE
class GoogleCSESearch(BaseSearchProvider):
    name = "google_cse"
    label = "Google Programmable Search"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        key = self.settings.google_cse_api_key
        cx = self.settings.google_cse_id
        if not key or not cx:
            raise SearchError(
                self.name,
                "GOOGLE_CSE_API_KEY / GOOGLE_CSE_ID are not both set",
                hint="Create an engine at https://programmablesearchengine.google.com/controlpanel/create",
            )
        params = {
            "key": key,
            "cx": cx,
            "q": query,
            "num": max(1, min(max_results, 10)),
        }
        try:
            async with self._client() as client:
                response = await client.get(
                    "https://www.googleapis.com/customsearch/v1", params=params
                )
        except httpx.HTTPError as exc:
            raise SearchError(self.name, f"network error: {type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise SearchError(
                self.name,
                f"HTTP {response.status_code}",
                status=response.status_code,
                hint="Free quota is 100 queries/day; check key + cx id.",
                body=response.text[:400],
            )
        data = response.json()
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                source=self.name,
            )
            for item in data.get("items", []) or []
        ][:max_results]


# ---------------------------------------------------------------- SearXNG
class SearXNGSearch(BaseSearchProvider):
    name = "searxng"
    label = "SearXNG"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        base_urls = [
            u.strip().rstrip("/")
            for u in (self.settings.searxng_base_urls or "").replace(";", ",").split(",")
            if u.strip()
        ]
        if not base_urls:
            raise SearchError(
                self.name,
                "SEARXNG_BASE_URLS is empty",
                hint="Self-host it (docker run searxng/searxng) and set "
                "SEARXNG_BASE_URLS=http://localhost:8080",
            )
        headers = {"X-API-Key": self.settings.searxng_api_key} if self.settings.searxng_api_key else None
        last_error: SearchError | None = None
        for base in base_urls:
            try:
                async with self._client() as client:
                    response = await client.get(
                        f"{base}/search",
                        params={"q": query, "format": "json"},
                        headers=headers,
                    )
            except httpx.HTTPError as exc:
                last_error = SearchError(
                    self.name,
                    f"cannot reach {base}: {type(exc).__name__}: {exc}",
                    hint="Is the SearXNG container actually running?",
                )
                continue
            if response.is_success:
                try:
                    data = response.json()
                except ValueError:
                    last_error = SearchError(
                        self.name,
                        f"{base} did not return JSON",
                        hint="Enable the JSON format in settings.yml (search.formats: [html, json]).",
                        body=response.text[:200],
                    )
                    continue
                return [
                    SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", ""),
                        published=item.get("publishedDate"),
                        source=self.name,
                    )
                    for item in data.get("results", [])
                ][:max_results]
            last_error = SearchError(
                self.name,
                f"{base} returned HTTP {response.status_code}",
                status=response.status_code,
                hint="Public instances often block the JSON API; self-host instead.",
                body=response.text[:200],
            )
        raise last_error or SearchError(self.name, "search failed")


PROVIDER_CLASSES = {
    "ddg": DuckDuckGoSearch,
    "wikipedia": WikipediaSearch,
    "tavily": TavilySearch,
    "serper": SerperSearch,
    "exa": ExaSearch,
    "google_cse": GoogleCSESearch,
    "searxng": SearXNGSearch,
}
