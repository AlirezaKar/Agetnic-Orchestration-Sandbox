"""
backend.agents — specialist worker agents.

``base``           Agent interface + shared AgentContext
``general_agent``  open-domain chat / writing / knowledge
``number_agent``   math, statistics, data analysis (with a safe calc tool)
``web_search_agent`` live search + grounded summarisation
``document_agent`` RAG over uploaded files
``registry``       builds the agent team from .env
"""

from .base import AgentContext, BaseAgent
from .registry import get_agent, get_agents, get_document_store, reset_agent_cache

__all__ = [
    "AgentContext",
    "BaseAgent",
    "get_agent",
    "get_agents",
    "get_document_store",
    "reset_agent_cache",
]
