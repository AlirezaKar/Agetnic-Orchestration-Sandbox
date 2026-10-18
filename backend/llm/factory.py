"""
backend.llm.factory
===================

Builds the right :class:`BaseLLMClient` for each role based on ``.env``:

* ``LLM_PROVIDER=groq``  → always Groq (fails loudly if key missing)
* ``LLM_PROVIDER=mock``  → always the offline mock
* ``LLM_PROVIDER=auto``  → Groq when ``GROQ_API_KEY`` is set, else mock

Two specialised clients are exposed:

``get_orchestrator_llm()``
    Manager + Planner reasoning (``GROQ_MODEL_ORCHESTRATOR``)
``get_agent_llm()``
    Worker agents (``GROQ_MODEL_AGENT``)
"""

from __future__ import annotations

from functools import lru_cache

from ..config import Settings, settings
from .base import BaseLLMClient, LLMError
from .groq_client import GroqClient
from .mock_client import MockLLMClient


def _build(settings: Settings, role: str) -> BaseLLMClient:
    provider = settings.effective_llm_provider

    if provider == "mock":
        return MockLLMClient(model="mock-llm")

    if provider == "groq":
        model = (
            settings.groq_model_orchestrator
            if role == "orchestrator"
            else settings.groq_model_agent
        )
        try:
            return GroqClient(settings, model=model)
        except LLMError:
            # A key can disappear at runtime; fail open to the mock so the
            # sandbox keeps working (the trace records `mocked: true`).
            return MockLLMClient(model="mock-llm-fallback")

    raise LLMError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'")


@lru_cache(maxsize=1)
def get_orchestrator_llm() -> BaseLLMClient:
    """LLM used by the Manager and the Planner."""
    return _build(settings, "orchestrator")


@lru_cache(maxsize=1)
def get_agent_llm() -> BaseLLMClient:
    """LLM used by all worker agents."""
    return _build(settings, "agent")


def reset_llm_cache() -> None:
    """Testing helper: drop cached clients (e.g. after changing env)."""
    get_orchestrator_llm.cache_clear()
    get_agent_llm.cache_clear()
