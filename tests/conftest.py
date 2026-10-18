"""
tests.conftest
==============

Shared test fixtures.

The whole suite runs against the **offline mock LLM** and the
**simulated search provider** so it passes on any machine — no API keys,
no internet.  Environment variables are set *before* any backend import,
which is exactly how ``backend.config`` reads them.
"""

from __future__ import annotations

import os

# ── Force deterministic offline providers for the entire suite ──
os.environ["LLM_PROVIDER"] = "mock"
os.environ["WEB_SEARCH_PROVIDER"] = "simulated"
os.environ["MAX_REPLAN_ATTEMPTS"] = "1"
os.environ["TASK_TIMEOUT_SECONDS"] = "5"

import pytest  # noqa: E402

from backend.agents.registry import get_agents, get_document_store  # noqa: E402
from backend.llm import get_orchestrator_llm, reset_llm_cache  # noqa: E402
from backend.orchestration.manager import Manager  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_llm():
    """Ensure every test sees the mock client (cache-safe)."""
    reset_llm_cache()
    yield
    reset_llm_cache()


@pytest.fixture
def manager() -> Manager:
    """A Manager wired with the mock LLM and real agents."""
    return Manager(get_orchestrator_llm(), get_agents())


@pytest.fixture
def document_store():
    """The process-wide RAG store (empty-ish; tests add their own docs)."""
    return get_document_store()
