"""The orchestrator's contract with the world outside it.

This module is the *port surface*: everything the orchestrator needs from its
host application, written as protocols instead of imports. Nothing here imports
another part of this repository, so the package can be lifted into a project
that has a different model client, a different search backend and a different
settings object — as long as they look like the shapes below.

Four ports:

| Port | What the orchestrator does with it |
|---|---|
| :class:`ModelChain` | `await chain.complete(messages, tools=None, ...)` for every planner / agent / review / synthesis call |
| :class:`SearchChain` | `await chain.search(query, limit)` — used by the search agent through the agent loop |
| :class:`AgentLoopFactory` | builds the single-agent tool loop the *search* agent delegates to |
| :class:`OrchestratorSettings` | the settings keys the orchestrator reads (a plain object with these attributes is enough) |

Nothing in the package calls a concrete class from this repository: the host
passes these four in (see ``service.py``), which is why the whole directory can
be copied into another project and wired in a dozen lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------- ports
@runtime_checkable
class ModelChain(Protocol):
    """Anything that can answer a chat completion.

    The real one is ``app.llm.registry.build_chain(settings)``; the tests pass a
    two-method fake. Only ``complete`` is used, and only with keyword arguments
    (``temperature``, ``max_tokens``) plus an optional ``tools`` list.
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any: ...


@runtime_checkable
class SearchChain(Protocol):
    """Anything that can run a web search query."""

    async def search(self, query: str, limit: int = 5) -> Any: ...


class AgentLoop(Protocol):
    """The single-agent tool loop (one model, tools, a trace) — a sibling service.

    The search agent delegates to it instead of re-implementing the loop. The
    real one is ``app.agent.WebSearchAgent``; another project brings its own.
    """

    async def run(
        self,
        message: str,
        *,
        history: list[dict[str, Any]] | None = None,
        allow_search: bool = True,
        max_steps: int | None = None,
    ) -> Any: ...


class AgentLoopFactory(Protocol):
    """How the orchestrator obtains an :class:`AgentLoop`.

    Called as ``factory(model_chain, search_chain, settings, observer=hook)``
    where ``observer(kind, payload)`` receives the loop's prompt/model/tool
    events so they can be written into the run trace.
    """

    def __call__(
        self,
        model_chain: Any,
        search_chain: Any,
        settings: Any,
        observer: Any = None,
    ) -> AgentLoop: ...


@runtime_checkable
class OrchestratorSettings(Protocol):
    """The settings the orchestrator reads.

    A dataclass, a pydantic model or a ``SimpleNamespace`` with these attributes
    is accepted — there is no import of ``app.config`` anywhere in the package.
    """

    # orchestration
    orchestrator_enabled: bool
    orchestrator_planner: str
    orchestrator_review: bool
    orchestrator_synthesize: bool
    orchestrator_agents: str
    orchestrator_roster: list[str]
    orchestrator_context_chars: int
    orchestrator_trace_chars: int
    orchestrator_timeout: float
    orchestrator_max_parallel: int
    orchestrator_log_runs: bool
    # model
    llm_temperature: float
    llm_max_tokens: int
    # agents
    agent_max_steps: int
    agent_max_search_results: int
    # attachments (the document agent)
    files_context_chars: int


# -------------------------------------------------------------------- input
@dataclass
class OrchestrationRequest:
    """One Auto-mode turn: the request, its files, and the per-request overrides."""

    message: str
    history: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[Any] = field(default_factory=list)
    agents: list[str] | None = None
    planner: str | None = None
    synthesize: bool | None = None
    review: bool | None = None

    @classmethod
    def from_payload(cls, payload: Any, *, attachments: list[Any] | None = None) -> "OrchestrationRequest":
        """Build from any object with the HTTP field names (a pydantic model)."""
        return cls(
            message=getattr(payload, "message", ""),
            history=list(getattr(payload, "history", None) or []),
            attachments=list(attachments if attachments is not None else getattr(payload, "attachments", None) or []),
            agents=getattr(payload, "agents", None),
            planner=getattr(payload, "planner", None),
            synthesize=getattr(payload, "synthesize", None),
            review=getattr(payload, "review", None),
        )
