"""
backend.config
==============

Central, *organized* configuration for the whole sandbox.

Every knob lives in ``.env`` (see ``.env.example`` for the annotated
template).  This module reads the environment exactly once and exposes a
frozen :class:`Settings` object so the rest of the code never touches
``os.getenv`` directly.

Sections
--------
1. LLM / Orchestrator  — provider, API keys, model names
2. Web search          — SearXNG (metasearch) + Crawl4AI (deep crawling)
3. Orchestration       — parallelism / re-planning / timeouts
4. Storage             — where traces and documents are written
5. Server              — host/port/CORS for the test harness
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Repository root = parent of the `backend/` package.  All relative paths
# in .env (e.g. TRACE_DIR=traces) are resolved against it so behaviour is
# identical no matter where the server is started from.
ROOT_DIR = Path(__file__).resolve().parent.parent

# Load .env from the repository root (existing environment variables win,
# so real deployment secrets can still be injected by the platform).
load_dotenv(ROOT_DIR / ".env")


def _get(name: str, default: str) -> str:
    """Read a string variable with a default."""
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


def _get_int(name: str, default: int) -> int:
    """Read an integer variable, falling back on parse errors."""
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    """Read a float variable, falling back on parse errors."""
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_list(name: str) -> List[str]:
    """Read a comma-separated list variable into a clean list."""
    raw = _get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve(path_str: str) -> Path:
    """Resolve a possibly-relative path against the repository root."""
    path = Path(path_str)
    return path if path.is_absolute() else ROOT_DIR / path


# ──────────────────────────────────────────────────────────────────
#  Settings
# ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of every configuration value."""

    # ── 1. LLM / Orchestrator ───────────────────────────────────
    llm_provider: str = "auto"            # auto | groq | mock
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model_orchestrator: str = "openai/gpt-oss-120b"
    groq_model_agent: str = "openai/gpt-oss-20b"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096

    # ── 2. Web search (open-source stack) ──────────────────────
    web_search_provider: str = "simulated"  # simulated | searxng
    # SearXNG — self-hosted metasearch engine (JSON API)
    searxng_base_url: str = "http://localhost:8888"
    searxng_api_key: str = ""               # optional (instances behind auth)
    searxng_language: str = ""              # e.g. "en", "de"; empty = instance default
    searxng_time_range: str = ""            # '' | day | month | year
    searxng_safesearch: int = 0             # 0 = off, 1 = moderate, 2 = strict
    # Crawl4AI — LLM-friendly deep crawling of the top search results
    crawl4ai_enabled: bool = True           # false → snippets only, no crawling
    crawl4ai_max_pages: int = 2             # how many top results to deep-crawl
    crawl4ai_max_content_chars: int = 6000  # per-page content cap for prompts/traces
    page_fetch_timeout: float = 20.0        # seconds per crawled page

    # ── 3. Orchestration ────────────────────────────────────────
    max_parallel_tasks: int = 4
    max_replan_attempts: int = 1
    max_plan_tasks: int = 8
    task_timeout_seconds: float = 90.0

    # ── 4. Storage ──────────────────────────────────────────────
    trace_dir: Path = field(default_factory=lambda: ROOT_DIR / "traces")
    document_dir: Path = field(default_factory=lambda: ROOT_DIR / "data" / "documents")

    # ── 5. Server ───────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    cors_extra_origins: List[str] = field(default_factory=list)

    # ── Derived helpers ─────────────────────────────────────────
    @property
    def effective_llm_provider(self) -> str:
        """Resolve ``auto`` → ``groq`` (key present) or ``mock``."""
        if self.llm_provider == "auto":
            return "groq" if self.groq_api_key else "mock"
        return self.llm_provider

    @property
    def cors_origins(self) -> List[str]:
        """Origins allowed to call the API from a browser."""
        base = ["*"]  # same-origin UI + permissive for the local sandbox
        return base + list(self.cors_extra_origins)

    def ensure_directories(self) -> None:
        """Create runtime directories if they are missing."""
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.document_dir.mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict:
        """Config snapshot safe to persist inside trace files / API output."""
        return {
            "llm_provider": self.effective_llm_provider,
            "llm_provider_configured": self.llm_provider,
            "groq_model_orchestrator": self.groq_model_orchestrator,
            "groq_model_agent": self.groq_model_agent,
            "web_search_provider": self.web_search_provider,
            "searxng_base_url": self.searxng_base_url,
            "crawl4ai_enabled": self.crawl4ai_enabled,
            "crawl4ai_max_pages": self.crawl4ai_max_pages,
            "max_parallel_tasks": self.max_parallel_tasks,
            "max_replan_attempts": self.max_replan_attempts,
            "max_plan_tasks": self.max_plan_tasks,
            "task_timeout_seconds": self.task_timeout_seconds,
            # Secrets are reported as booleans only — never leaked.
            "groq_api_key_set": bool(self.groq_api_key),
            "searxng_api_key_set": bool(self.searxng_api_key),
        }


def load_settings() -> Settings:
    """Build the :class:`Settings` snapshot from the environment."""
    settings = Settings(
        llm_provider=_get("LLM_PROVIDER", "auto").lower(),
        groq_api_key=_get("GROQ_API_KEY", ""),
        groq_base_url=_get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        groq_model_orchestrator=_get("GROQ_MODEL_ORCHESTRATOR", "openai/gpt-oss-120b"),
        groq_model_agent=_get("GROQ_MODEL_AGENT", "openai/gpt-oss-20b"),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.2),
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", 4096),
        web_search_provider=_get("WEB_SEARCH_PROVIDER", "simulated").lower(),
        searxng_base_url=_get("SEARXNG_BASE_URL", "http://localhost:8888"),
        searxng_api_key=_get("SEARXNG_API_KEY", ""),
        searxng_language=_get("SEARXNG_LANGUAGE", ""),
        searxng_time_range=_get("SEARXNG_TIME_RANGE", "").lower(),
        searxng_safesearch=_get_int("SEARXNG_SAFESEARCH", 0),
        crawl4ai_enabled=_get("CRAWL4AI_ENABLED", "true").lower() not in ("false", "0", "no"),
        crawl4ai_max_pages=_get_int("CRAWL4AI_MAX_PAGES", 2),
        crawl4ai_max_content_chars=_get_int("CRAWL4AI_MAX_CONTENT_CHARS", 6000),
        page_fetch_timeout=_get_float("PAGE_FETCH_TIMEOUT", 20.0),
        max_parallel_tasks=_get_int("MAX_PARALLEL_TASKS", 4),
        max_replan_attempts=_get_int("MAX_REPLAN_ATTEMPTS", 1),
        max_plan_tasks=_get_int("MAX_PLAN_TASKS", 8),
        task_timeout_seconds=_get_float("TASK_TIMEOUT_SECONDS", 90.0),
        trace_dir=_resolve(_get("TRACE_DIR", "traces")),
        document_dir=_resolve(_get("DOCUMENT_DIR", "data/documents")),
        host=_get("HOST", "0.0.0.0"),
        port=_get_int("PORT", 8000),
        cors_extra_origins=_get_list("CORS_EXTRA_ORIGINS"),
    )
    settings.ensure_directories()
    return settings


# Process-wide singleton — import `settings` anywhere for convenience.
settings = load_settings()
