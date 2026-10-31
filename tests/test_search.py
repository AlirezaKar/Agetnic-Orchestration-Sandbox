"""Web-search tests: HTML parsing, failover, empty-vs-error, and guard rails."""

from __future__ import annotations

import unittest

import httpx

from app.config import Settings
from app.search.base import SearchError, SearchResult
from app.search.fetch import fetch_page, html_to_text
from app.search.providers import (
    DuckDuckGoSearch,
    TavilySearch,
    WikipediaSearch,
    parse_ddg_html,
)
from app.search.registry import SearchChain, build_search_chain, dedupe_results

DDG_PAGE = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html&amp;rut=xyz">
         asyncio &mdash; Asynchronous I/O
      </a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org">
      asyncio is a library to write <b>concurrent</b> code using async/await.
    </a>
  </div>
</div>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a class="result__a" href="https://realpython.com/async-io-python/">Async IO in Python</a>
    </h2>
    <div class="result__snippet">A complete walkthrough of async IO.</div>
  </div>
</div>
<div class="result result--ad">
  <h2 class="result__title">
    <a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=bing">Buy something</a>
  </h2>
</div>
</body></html>
"""

DDG_LITE_PAGE = """
<html><body><table>
<tr><td><a class="result-link" href="https://example.com/lite">Lite result</a></td></tr>
<tr><td class="result-snippet">Snippet from the lite endpoint.</td></tr>
</table></body></html>
"""


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class DDGParsingTests(unittest.TestCase):
    def test_parses_titles_snippets_and_decodes_redirects(self):
        results = parse_ddg_html(DDG_PAGE)
        self.assertEqual(len(results), 2)  # the ad row is dropped
        self.assertEqual(results[0].url, "https://docs.python.org/3/library/asyncio.html")
        self.assertEqual(results[0].title, "asyncio — Asynchronous I/O")
        self.assertIn("concurrent code", results[0].snippet)
        self.assertEqual(results[1].url, "https://realpython.com/async-io-python/")
        self.assertEqual(results[1].snippet, "A complete walkthrough of async IO.")

    def test_parses_lite_layout(self):
        results = parse_ddg_html(DDG_LITE_PAGE)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/lite")
        self.assertEqual(results[0].snippet, "Snippet from the lite endpoint.")

    def test_empty_page(self):
        self.assertEqual(parse_ddg_html("<html><body>no results</body></html>"), [])

class DDGTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_challenge_is_detected(self):
        provider = DuckDuckGoSearch(settings())
        provider._transport = httpx.MockTransport(
            lambda request: httpx.Response(
                202, text="<html><div class='anomaly-modal'>Unusual traffic</div></html>"
            )
        )
        provider.LITE_ENDPOINT = provider.HTML_ENDPOINT  # both attempts hit the mock
        with self.assertRaises(SearchError) as ctx:
            await provider.search("python")
        self.assertIn("bot challenge", ctx.exception.message)
        self.assertIn("tavily", ctx.exception.hint)

    async def test_successful_ddg_search(self):
        provider = DuckDuckGoSearch(settings())
        provider._transport = httpx.MockTransport(lambda request: httpx.Response(200, text=DDG_PAGE))
        results = await provider.search("python asyncio", max_results=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "ddg")

    async def test_network_error_is_wrapped_with_hint(self):
        def boom(request):
            raise httpx.ConnectError("no route to host")

        provider = DuckDuckGoSearch(settings())
        provider._transport = httpx.MockTransport(boom)
        provider.LITE_ENDPOINT = provider.HTML_ENDPOINT
        with self.assertRaises(SearchError) as ctx:
            await provider.search("python")
        self.assertIn("network error", ctx.exception.message)


class WikipediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_mediawiki_response(self):
        payload = {
            "query": {
                "search": [
                    {"title": "Asyncio", "snippet": "<span class='searchmatch'>asyncio</span> is a library"},
                ]
            }
        }
        provider = WikipediaSearch(settings())
        provider._transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        results = await provider.search("asyncio", max_results=3)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://en.wikipedia.org/wiki/Asyncio")
        self.assertEqual(results[0].snippet, "asyncio is a library")

    async def test_honours_language_setting(self):
        seen = {}

        def handler(request):
            seen["host"] = request.url.host
            return httpx.Response(200, json={"query": {"search": []}})

        provider = WikipediaSearch(settings(wikipedia_lang="de"))
        provider._transport = httpx.MockTransport(handler)
        await provider.search("asyncio")
        self.assertEqual(seen["host"], "de.wikipedia.org")


class KeyedProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_tavily_without_key_explains_where_to_get_one(self):
        with self.assertRaises(SearchError) as ctx:
            await TavilySearch(settings()).search("q")
        self.assertIn("TAVILY_API_KEY", str(ctx.exception))
        self.assertIn("tavily.com", ctx.exception.hint)

    async def test_tavily_401_hint(self):
        provider = TavilySearch(settings(tavily_api_key="tvly-bad"))
        provider._transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "bad key"}))
        with self.assertRaises(SearchError) as ctx:
            await provider.search("q")
        self.assertIn("app.tavily.com", ctx.exception.hint)

    async def test_tavily_parses_results(self):
        payload = {
            "results": [
                {"title": "T", "url": "https://x.test/a", "content": "body", "published_date": "2026-01-01"}
            ]
        }
        provider = TavilySearch(settings(tavily_api_key="tvly-ok"))
        provider._transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        results = await provider.search("q")
        self.assertEqual(results[0].snippet, "body")
        self.assertEqual(results[0].published, "2026-01-01")

    async def test_missing_key_providers_are_skipped_by_the_registry(self):
        chain = build_search_chain(settings(search_providers="tavily,ddg"))
        self.assertEqual([p.name for p in chain.providers], ["ddg"])
        self.assertIn("TAVILY_API_KEY", chain.skipped[0])

    async def test_unknown_provider_name_is_skipped_not_crashing(self):
        chain = build_search_chain(settings(search_providers="nope,ddg"))
        self.assertEqual([p.name for p in chain.providers], ["ddg"])
        self.assertIn("unknown search provider", chain.skipped[0])


class FakeProvider:
    def __init__(self, name, behaviour):
        self.name = name
        self._behaviour = behaviour
        self.calls = 0

    async def search(self, query, max_results=5):
        self.calls += 1
        outcome = self._behaviour
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ChainTests(unittest.IsolatedAsyncioTestCase):
    def chain(self, *behaviours):
        providers = [FakeProvider(f"p{i}", b) for i, b in enumerate(behaviours)]
        return SearchChain(providers, settings())

    async def test_first_non_empty_wins(self):
        chain = self.chain(
            [SearchResult(title="a", url="https://a.test", source="p0")],
            [SearchResult(title="b", url="https://b.test", source="p1")],
        )
        response = await chain.search("q")
        self.assertEqual(response.provider, "p0")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(chain.providers[1].calls, 0)

    async def test_failover_on_error(self):
        chain = self.chain(
            SearchError("p0", "429"),
            [SearchResult(title="b", url="https://b.test", source="p1")],
        )
        response = await chain.search("q")
        self.assertEqual(response.provider, "p1")
        self.assertEqual(response.attempts[0]["ok"], False)

    async def test_empty_result_falls_through(self):
        chain = self.chain(
            [],
            [SearchResult(title="b", url="https://b.test", source="p1")],
        )
        response = await chain.search("q")
        self.assertEqual(response.provider, "p1")

    async def test_all_errors_raises_with_detail(self):
        chain = self.chain(SearchError("p0", "boom"), SearchError("p1", "boom too"))
        with self.assertRaises(SearchError) as ctx:
            await chain.search("q")
        self.assertIn("p0", str(ctx.exception))
        self.assertIn("p1", str(ctx.exception))

    async def test_all_empty_returns_empty_not_error(self):
        chain = self.chain([], [])
        response = await chain.search("q")
        self.assertEqual(response.results, [])
        self.assertEqual(len(response.attempts), 2)

    async def test_dedupe_keeps_first_occurrence(self):
        results = [
            SearchResult(title="a", url="https://www.example.com/x/"),
            SearchResult(title="dup", url="https://example.com/x"),
            SearchResult(title="b", url="https://other.test/y"),
        ]
        out = dedupe_results(results, 10)
        self.assertEqual([r.title for r in out], ["a", "b"])
        self.assertEqual([r.rank for r in out], [1, 2])

    async def test_max_results_is_enforced(self):
        results = [SearchResult(title=str(i), url=f"https://x{i}.test") for i in range(9)]
        chain = self.chain(results)
        response = await chain.search("q", max_results=3)
        self.assertEqual(len(response.results), 3)

    async def test_empty_chain_explains_configuration(self):
        with self.assertRaises(SearchError) as ctx:
            await SearchChain([], settings()).search("q")
        self.assertIn("SEARCH_PROVIDER", str(ctx.exception))

    async def test_ping_shape(self):
        provider = FakeProvider("ddg", [SearchResult(title="a", url="https://a.test")])
        report = await provider_report(provider)
        self.assertTrue(report["ok"])
        self.assertEqual(report["count"], 1)


async def provider_report(provider):
    # BaseSearchProvider.ping is only defined on real providers; replicate it.
    from app.search.providers import BaseSearchProvider

    class _Wrapper(BaseSearchProvider):
        name = provider.name

        async def search(self, query, max_results=5):
            return await provider.search(query, max_results)

    return await _Wrapper(settings()).ping()


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def test_html_to_text_strips_markup(self):
        text = html_to_text("<h1>Title</h1><p>Body <b>bold</b></p><script>x()</script>")
        self.assertIn("Title", text)
        self.assertIn("Body bold", text)
        self.assertNotIn("x()", text)

    async def test_private_hosts_are_refused(self):
        for url in ("http://localhost:8000/x", "http://127.0.0.1/", "http://10.0.0.5/"):
            with self.assertRaises(SearchError):
                await fetch_page(url, settings())

    async def test_bad_scheme_is_refused(self):
        with self.assertRaises(SearchError):
            await fetch_page("file:///etc/passwd", settings())

    async def test_jina_then_direct_fallback(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if str(request.url).startswith("https://r.jina.ai/"):
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, text="<html><body><p>Direct body</p></body></html>")

        page = await fetch_page(
            "https://example.com/doc", settings(), transport=httpx.MockTransport(handler)
        )
        self.assertEqual(page["source"], "direct-http")
        self.assertIn("Direct body", page["content"])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
