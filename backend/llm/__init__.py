"""
backend.llm — LLM provider abstraction.

``base``    interface + message/response types
``groq_client``  real Groq (OpenAI-compatible) HTTP client
``mock_client``  deterministic offline model for keyless testing
``factory``      provider selection from .env
"""

from .base import BaseLLMClient, LLMError, LLMMessage, LLMResponse
from .factory import get_agent_llm, get_orchestrator_llm, reset_llm_cache

__all__ = [
    "BaseLLMClient",
    "LLMError",
    "LLMMessage",
    "LLMResponse",
    "get_agent_llm",
    "get_orchestrator_llm",
    "reset_llm_cache",
]
