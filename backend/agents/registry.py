"""
backend.agents.registry
=======================

Builds the agent team from configuration.

The Manager never constructs agents itself — it asks this registry — so
adding a new specialist later means: implement :class:`BaseAgent`, add a
line here, add an enum value, done.  Nothing in the orchestration layer
changes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict

from ..config import settings
from ..llm import get_agent_llm
from ..rag.store import DocumentStore
from ..schemas import AgentName
from ..search import get_search_provider
from .base import BaseAgent
from .document_agent import DocumentAgent
from .general_agent import GeneralAgent
from .number_agent import NumberAgent
from .web_search_agent import WebSearchAgent


@lru_cache(maxsize=1)
def get_document_store() -> DocumentStore:
    """Process-wide RAG store (lazily loaded from disk)."""
    return DocumentStore(document_dir=settings.document_dir)


@lru_cache(maxsize=1)
def get_agents() -> Dict[AgentName, BaseAgent]:
    """Instantiate every worker agent with its dependencies."""
    llm = get_agent_llm()
    return {
        AgentName.DOCUMENT: DocumentAgent(llm, get_document_store()),
        AgentName.WEB_SEARCH: WebSearchAgent(llm, get_search_provider()),
        AgentName.GENERAL: GeneralAgent(llm),
        AgentName.NUMBER: NumberAgent(llm),
    }


def get_agent(name: AgentName) -> BaseAgent:
    """Fetch one agent by name (raises ``KeyError`` on unknown names)."""
    return get_agents()[name]


def reset_agent_cache() -> None:
    """Testing helper: drop cached agents/stores."""
    get_agents.cache_clear()
    get_document_store.cache_clear()
