"""
tests.test_search — the open-source web-search stack
(SearXNG provider · Crawl4AI fetcher · simple fallback · agent wiring).
"""

import asyncio

import httpx
import pytest

from backend.config import Settings, settings
from backend.search.base import PageContent, SearchResponse, SearchResult
from backend.search.crawl4ai_fetcher import Crawl4AIFetcher
from backend.search.factory import get_page_fetcher, get_search_provider
from backend.search.searxng import SearXNGSearchProvider, parse_searxng_payload
from backend.search.simple_fetcher import SimpleHttpFetcher, extract_title, html_to_text
from backend.search.simulated import SimulatedSearchProvider


# ──────────────────────────────────────────────────────────────────
#  SearXNG payload parsing (pure function — no network)
# ──────────────────────────────────────────────────────────────────
SAMPLE_SEARXNG_PAYLOAD = {
    "query": "llm orchestration",
    "number_of_results": 2,
    "results": [
        {
            "title": "Orchestrating LLM Agents",
            "url": "https://example.org/agents",
            "content": "A practical guide to multi-agent orchestration.",
            "engines": ["google", "duckduckgo"],
            "score": 3.14,
            "publishedDate": "2026-01-01",
        },
        {
            "title": "SearXNG docs",
            "url": "https://docs.searxng.org",
            "content": "Metasearch engine documentation.",
            "engines": ["bing"],
            "score": 2.0,
        },
    ],
}


def test_parse_searxng_payload():
    resp = parse_searxng_payload(SAMPLE_SEARXNG_PAYLOAD, "llm orchestration", 5)
    assert isinstance(resp, SearchResponse)
    assert resp.simulated is False
    assert len(resp.results) == 2
    first = resp.results[0]
    assert first.title == "Orchestrating LLM Agents"
    assert first.url == "https://example.org/agents"
    assert first.snippet.startswith("A practical guide")
    assert first.engine == "google, duckduckgo"
    assert first.score == 3.14


def test_parse_searxng_payload_caps_results():
    resp = parse_searxng_payload(SAMPLE_SEARXNG_PAYLOAD, "q", max_results=1)
    assert len(resp.results) == 1


def test_parse_searxng_empty_payload():
    resp = parse_searxng_payload({}, "q", 5)
    assert resp.results == []


# ──────────────────────────────────────────────────────────────────
#  SearXNG provider (httpx mocked via MockTransport)
# ──────────────────────────────────────────────────────────────────
def _provider_with_transport(handler) -> SearXNGSearchProvider:
    provider = SearXNGSearchProvider(settings)
    return provider


async def test_searxng_search_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        assert request.url.params["q"] == "hello"
        return httpx.Response(200, json=SAMPLE_SEARXNG_PAYLOAD)

    provider = SearXNGSearchProvider(Settings(searxng_base_url="http://searx.test"))

    # Replace the provider's HTTP call with a MockTransport round-trip.
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("backend.search.searxng.httpx.AsyncClient", fake_client)
    response = await provider.search("hello", max_results=5)
    assert response.provider == "searxng"
    assert len(response.results) == 2


async def test_searxng_403_means_json_format_disabled(monkeypatch):
    from backend.search.base import SearchError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    provider = SearXNGSearchProvider(Settings(searxng_base_url="http://searx.test"))
    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("backend.search.searxng.httpx.AsyncClient", fake_client)
    with pytest.raises(SearchError, match="JSON output format"):
        await provider.search("hello")


def test_searxng_auth_headers():
    provider = SearXNGSearchProvider(Settings(searxng_api_key="k123"))
    headers = provider._headers()
    assert headers["Authorization"] == "Bearer k123"
    assert headers["X-API-Key"] == "k123"


# ──────────────────────────────────────────────────────────────────
#  Simple HTTP fetcher (HTML → text)
# ──────────────────────────────────────────────────────────────────
def test_html_to_text_strips_markup():
    source = """
    <html><head><title>  My Page  </title><script>evil()</script>
    <style>.x{color:red}</style></head>
    <body><!-- comment --><h1>Hello</h1><p>World &amp; friends</p></body></html>
    """
    text = html_to_text(source)
    assert "Hello" in text and "World & friends" in text
    assert "evil" not in text and "color:red" not in text and "comment" not in text
    assert extract_title(source) == "My Page"


async def test_simple_fetcher_ok_and_error(monkeypatch):
    fetcher = SimpleHttpFetcher(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if "good" in str(request.url):
            return httpx.Response(200, text="<html><title>T</title><body>content here</body></html>")
        return httpx.Response(404, text="nope")

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("backend.search.simple_fetcher.httpx.AsyncClient", fake_client)

    ok = await fetcher.fetch("https://example.com/good")
    assert ok.ok and ok.title == "T" and "content here" in ok.markdown
    assert ok.fetcher == "simple_http"

    bad = await fetcher.fetch("https://example.com/bad")
    assert bad.ok is False and bad.error


# ──────────────────────────────────────────────────────────────────
#  Crawl4AI fetcher
# ──────────────────────────────────────────────────────────────────
def test_crawl4ai_availability_probe():
    # Whether or not crawl4ai is installed here, the probe must not raise.
    assert isinstance(Crawl4AIFetcher.is_available(), bool)


async def test_crawl4ai_fetch_error_is_captured_not_raised(monkeypatch):
    """Browser/startup errors must degrade cleanly to ok=False without raising."""
    import sys
    import types

    fake_mod = types.ModuleType("crawl4ai")
    fake_mod.CacheMode = types.SimpleNamespace(ENABLED="enabled")
    fake_mod.CrawlerRunConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "crawl4ai", fake_mod)

    fetcher = Crawl4AIFetcher(Settings(crawl4ai_max_content_chars=100))

    async def broken_browser():
        raise RuntimeError("Playwright browser unavailable")

    monkeypatch.setattr(fetcher, "_get_crawler", broken_browser)
    page = await fetcher.fetch("https://example.com/whatever")

    assert isinstance(page, PageContent)
    assert page.fetcher == "crawl4ai"
    assert page.ok is False
    assert page.error and "Playwright browser unavailable" in page.error


async def test_crawl4ai_fetch_truncates_and_closes(monkeypatch):
    """Successful crawl extracts title, truncates markdown, and closes cleanly."""
    import sys
    import types

    fake_mod = types.ModuleType("crawl4ai")
    fake_mod.CacheMode = types.SimpleNamespace(ENABLED="enabled")
    fake_mod.CrawlerRunConfig = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "crawl4ai", fake_mod)

    closed = False

    class FakeBrowser:
        async def arun(self, *, url, config):
            return types.SimpleNamespace(
                success=True,
                markdown="x" * 250,
                metadata={"title": "Fetched Title"},
            )

        async def __aexit__(self, exc_type, exc, tb):
            nonlocal closed
            closed = True

    fetcher = Crawl4AIFetcher(Settings(crawl4ai_max_content_chars=80))
    fetcher._crawler = FakeBrowser()

    page = await fetcher.fetch("https://example.com/doc")
    assert page.ok is True
    assert page.title == "Fetched Title"
    assert page.markdown.startswith("x" * 80)
    assert "[…content truncated…]" in page.markdown

    await fetcher.close()
    assert closed is True
    assert fetcher._crawler is None


# ──────────────────────────────────────────────────────────────────
#  Factory selection
# ──────────────────────────────────────────────────────────────────
def test_factory_selects_simulated_by_default(monkeypatch):
    from backend.search import factory

    monkeypatch.setattr(factory, "settings", Settings(web_search_provider="simulated"))
    factory.get_search_provider.cache_clear()
    assert isinstance(factory.get_search_provider(), SimulatedSearchProvider)
    factory.get_search_provider.cache_clear()


def test_factory_selects_searxng(monkeypatch):
    from backend.search import factory

    monkeypatch.setattr(factory, "settings", Settings(web_search_provider="searxng"))
    factory.get_search_provider.cache_clear()
    assert isinstance(factory.get_search_provider(), SearXNGSearchProvider)
    factory.get_search_provider.cache_clear()


def test_factory_fetcher_disabled_returns_none(monkeypatch):
    from backend.search import factory

    monkeypatch.setattr(factory, "settings", Settings(crawl4ai_enabled=False))
    factory.get_page_fetcher.cache_clear()
    assert factory.get_page_fetcher() is None
    factory.get_page_fetcher.cache_clear()


def test_factory_fetcher_fallback_when_unavailable(monkeypatch):
    from backend.search import factory

    monkeypatch.setattr(factory, "settings", Settings(crawl4ai_enabled=True))
    monkeypatch.setattr(Crawl4AIFetcher, "is_available", staticmethod(lambda: False))
    factory.get_page_fetcher.cache_clear()
    assert isinstance(factory.get_page_fetcher(), SimpleHttpFetcher)
    factory.get_page_fetcher.cache_clear()


# ──────────────────────────────────────────────────────────────────
#  Web-search agent + deep crawl (fake provider & fetcher)
# ──────────────────────────────────────────────────────────────────
class FakeSearchProvider:
    name = "fake"

    async def search(self, query, *, max_results=5):
        return SearchResponse(
            query=query,
            provider="fake",
            simulated=False,
            results=[
                SearchResult(title="A", url="https://a.example/1", snippet="alpha"),
                SearchResult(title="B", url="https://b.example/2", snippet="beta"),
            ],
        )


class FakeFetcher:
    name = "crawl4ai"
    calls: list = []

    async def fetch(self, url):
        FakeFetcher.calls.append(url)
        return PageContent(url=url, ok=True, title="Page", markdown="deep content", fetcher=self.name)


async def test_web_search_agent_deep_crawls_real_results(monkeypatch):
    from backend.agents.base import AgentContext
    from backend.agents.web_search_agent import WebSearchAgent
    from backend.llm.mock_client import MockLLMClient

    monkeypatch.setattr("backend.agents.web_search_agent.settings", settings)
    FakeFetcher.calls = []
    agent = WebSearchAgent(MockLLMClient(), FakeSearchProvider(), FakeFetcher())
    result = await agent.run('Search the web for: "something fresh"', AgentContext())

    assert result.success
    assert len(FakeFetcher.calls) == 2  # CRAWL4AI_MAX_PAGES default = 2
    assert len(result.data["crawled_pages"]) == 2
    assert result.data["page_fetcher"] == "crawl4ai"


async def test_web_search_agent_skips_crawl_for_simulated(monkeypatch):
    """Simulated results must never be deep-crawled (fake URLs)."""
    from backend.agents.base import AgentContext
    from backend.agents.web_search_agent import WebSearchAgent
    from backend.llm.mock_client import MockLLMClient

    class SimProvider:
        async def search(self, query, *, max_results=5):
            return SearchResponse(
                query=query, provider="simulated", simulated=True,
                results=[SearchResult(title="S", url="https://simulated.example/1", snippet="s")],
            )

    FakeFetcher.calls = []
    agent = WebSearchAgent(MockLLMClient(), SimProvider(), FakeFetcher())
    result = await agent.run('Search the web for: "anything"', AgentContext())
    assert FakeFetcher.calls == []
    assert result.data["crawled_pages"] == []
