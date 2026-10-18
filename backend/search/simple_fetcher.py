"""
backend.search.simple_fetcher
=============================

Lightweight **fallback page fetcher**: plain httpx + HTML→text.

Used automatically when ``crawl4ai`` is not installed (or is disabled).
It doesn't render JavaScript, but for server-rendered pages it provides
the same :class:`PageContent` contract — so the Web-search agent and the
traces look identical either way.
"""

from __future__ import annotations

import html as html_lib
import re

import httpx

from ..config import Settings
from .base import PageContent, PageFetcher

# Crude but dependency-free HTML→text pipeline.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg|header|footer|nav)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_text(source: str) -> str:
    """
    Strip markup into readable plain text (testable in isolation).

    Steps: drop script/style/svg blocks & comments → drop tags →
    unescape entities → tidy whitespace.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", source)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def extract_title(source: str) -> str:
    """Pull the ``<title>`` out of a document (best effort)."""
    match = _TITLE_RE.search(source)
    return html_lib.unescape(match.group(1)).strip() if match else ""


class SimpleHttpFetcher(PageFetcher):
    """Fetch pages with a plain HTTP GET (no JS rendering)."""

    name = "simple_http"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch(self, url: str) -> PageContent:
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.page_fetch_timeout,
                follow_redirects=True,
                headers={"User-Agent": "AgetnicOrchestrationSandbox/1.0"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                source = response.text
        except Exception as exc:  # noqa: BLE001 — fetch errors are data
            return PageContent(url=url, ok=False, fetcher=self.name,
                               error=f"{type(exc).__name__}: {exc}")

        text = html_to_text(source)
        max_chars = self._settings.crawl4ai_max_content_chars
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[…content truncated…]"

        return PageContent(
            url=url,
            ok=True,
            title=extract_title(source),
            markdown=text,
            fetcher=self.name,
        )
