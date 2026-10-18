"""
backend.search — pluggable web-search providers.

``base``        provider protocol + SearchResult
``simulated``   deterministic offline provider (default)
``factory``     provider selection from .env (simulated/tavily/serper)
"""

from .base import SearchProvider, SearchResult
from .factory import get_search_provider

__all__ = ["SearchProvider", "SearchResult", "get_search_provider"]
