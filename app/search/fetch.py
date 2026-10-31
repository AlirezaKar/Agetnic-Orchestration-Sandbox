"""Read a page as text — no browser, no background service.

Two free strategies, tried in order:

1. ``r.jina.ai`` reader (keyless; add JINA_API_KEY for higher limits) returns
   clean Markdown for almost any page.
2. Plain HTTP GET + tag stripping, for when the reader is rate limited.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.search.base import SearchError

_BLOCK_TAGS = re.compile(r"(?i)</(p|div|li|h[1-6]|tr|br|section|article)>")
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _is_private_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host in {"localhost", "localhost.localdomain", "::1"} or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def fetch_page(
    url: str,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SearchError("fetch", f"unsupported URL scheme: {parsed.scheme or 'none'}")
    if _is_private_host(parsed.hostname or ""):
        raise SearchError(
            "fetch",
            "refusing to fetch a private/loopback address",
            hint="The reader tool only reaches the public web.",
        )

    limit = max_chars or settings.fetch_max_chars
    timeout = settings.search_timeout + 10
    headers = {"User-Agent": settings.search_user_agent}

    # 1) Jina reader
    try:
        jina_headers = {"Accept": "text/plain", **headers}
        if settings.jina_api_key:
            jina_headers["Authorization"] = f"Bearer {settings.jina_api_key}"
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport, follow_redirects=True
        ) as client:
            response = await client.get(f"https://r.jina.ai/{url}", headers=jina_headers)
        if response.is_success and response.text.strip():
            text = response.text.strip()
            return {
                "url": url,
                "content": text[:limit],
                "truncated": len(text) > limit,
                "chars": min(len(text), limit),
                "source": "jina-reader",
            }
    except httpx.HTTPError:
        pass

    # 2) raw fetch
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport, follow_redirects=True
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise SearchError(
            "fetch", f"network error: {type(exc).__name__}: {exc}"
        ) from exc
    if not response.is_success:
        raise SearchError(
            "fetch",
            f"HTTP {response.status_code}",
            status=response.status_code,
            hint="The site may block non-browser clients.",
        )
    text = html_to_text(response.text)
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.splitlines() if line.strip())
    return {
        "url": url,
        "content": text[:limit],
        "truncated": len(text) > limit,
        "chars": min(len(text), limit),
        "source": "direct-http",
    }
