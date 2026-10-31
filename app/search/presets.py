"""Catalogue of web-search backends.

``needs_server=False`` means nothing has to run in the background: the backend
talks straight to a public HTTPS endpoint.

Keyless options (truly nothing to run, nothing to sign up for):
    ddg        DuckDuckGo HTML endpoint. Unofficial; can hit bot challenges.
    wikipedia  MediaWiki API. Narrow (encyclopedia only) but rock solid.

Free-key options (a free key, still no server):
    tavily      1,000 searches/month free.
    serper      2,500 one-off free credits (Google results).
    exa         1,000 searches/month free (neural search).
    google_cse  100 queries/day free (official Google index).

Server option:
    searxng   self-hosted metasearch. Free and unlimited, but it is a process
              you have to keep running, so it is *not* the default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchPreset:
    name: str
    label: str
    key_env: tuple[str, ...]
    key_url: str
    docs_url: str
    free_tier: str
    needs_server: bool
    needs_key: bool
    notes: str


SEARCH_PRESETS: dict[str, SearchPreset] = {
    "ddg": SearchPreset(
        name="ddg",
        label="DuckDuckGo (HTML, no key)",
        key_env=(),
        key_url="(none needed)",
        docs_url="https://duckduckgo.com/html/",
        free_tier="Free forever, no account, no quota.",
        needs_server=False,
        needs_key=False,
        notes="Unofficial HTML endpoint. Datacenter/VPS IPs sometimes get a bot "
        "challenge page; the client detects it and fails over to the next provider.",
    ),
    "wikipedia": SearchPreset(
        name="wikipedia",
        label="Wikipedia (MediaWiki API, no key)",
        key_env=(),
        key_url="(none needed)",
        docs_url="https://www.mediawiki.org/wiki/API:Search",
        free_tier="Free, no account, generous rate limits.",
        needs_server=False,
        needs_key=False,
        notes="Encyclopedic facts only. Great zero-setup fallback behind ddg.",
    ),
    "tavily": SearchPreset(
        name="tavily",
        label="Tavily",
        key_env=("TAVILY_API_KEY",),
        key_url="https://app.tavily.com/home",
        docs_url="https://docs.tavily.com/documentation/api-reference/endpoint/search",
        free_tier="1,000 credits/month free (no card).",
        needs_server=False,
        needs_key=True,
        notes="Built for agents: clean JSON, snippets ready to feed an LLM.",
    ),
    "serper": SearchPreset(
        name="serper",
        label="Serper (Google SERP)",
        key_env=("SERPER_API_KEY",),
        key_url="https://serper.dev/api-key",
        docs_url="https://serper.dev/playground",
        free_tier="2,500 free credits on signup.",
        needs_server=False,
        needs_key=True,
        notes="Real Google results. Free credits are one-off, then paid.",
    ),
    "exa": SearchPreset(
        name="exa",
        label="Exa",
        key_env=("EXA_API_KEY",),
        key_url="https://dashboard.exa.ai/api-keys",
        docs_url="https://docs.exa.ai/reference/search",
        free_tier="1,000 searches/month free.",
        needs_server=False,
        needs_key=True,
        notes="Neural/semantic search, good for research-style queries.",
    ),
    "google_cse": SearchPreset(
        name="google_cse",
        label="Google Programmable Search (CSE)",
        key_env=("GOOGLE_CSE_API_KEY", "GOOGLE_CSE_ID"),
        key_url="https://developers.google.com/custom-search/v1/introduction",
        docs_url="https://developers.google.com/custom-search/v1/using_rest",
        free_tier="100 queries/day free.",
        needs_server=False,
        needs_key=True,
        notes="Official Google index. Needs two values: an API key and a search "
        "engine ID (cx) from https://programmablesearchengine.google.com/controlpanel/.",
    ),
    "searxng": SearchPreset(
        name="searxng",
        label="SearXNG (self-hosted)",
        key_env=("SEARXNG_BASE_URLS",),
        key_url="(no key; you host it)",
        docs_url="https://docs.searxng.org/dev/search_api.html",
        free_tier="Free and unlimited, but you run the container.",
        needs_server=True,
        needs_key=False,
        notes="Only option here that needs a background server. Public instances "
        "usually disable the JSON API, so self-hosting is the reliable path.",
    ),
}


def get_search_preset(name: str) -> SearchPreset | None:
    return SEARCH_PRESETS.get((name or "").strip().lower())
