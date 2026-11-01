"""The orchestrator exposed as a service: one entry point, four injected ports.

A host application (this one, or another with a similar architecture) does:

```python
service = OrchestratorService(
    settings,                       # any object shaped like contracts.OrchestratorSettings
    models=build_chain(settings),   # contracts.ModelChain
    search=build_search_chain(settings),   # contracts.SearchChain
    agent_loop=WebSearchAgent,      # contracts.AgentLoopFactory (optional: defaults to host)
)
result = await service.run(OrchestrationRequest(message="…", attachments=[…]))
body = service.payload(result)      # JSON for the browser, clipped for the trace
```

Inside, the service is exactly the manager that shipped in v0.8.0 — classify →
plan → parallel waves → review → synthesis — with the same trace, the same run
log and the same timings. Nothing about the algorithm lives here: this module
only assembles the collaborators, so the rest of the package never has to import
the host.
"""

from __future__ import annotations

import os
from typing import Any

from app.orchestrator import host
from app.orchestrator.agents import catalogue
from app.orchestrator.contracts import OrchestrationRequest, OrchestratorSettings
from app.orchestrator.manager import Manager, OrchestrationResult, agent_catalogue

#: sentinel: "use ORCHESTRATOR_TRACE_CHARS" (``None`` still means *no* clipping)
_DEFAULT = object()


class OrchestratorService:
    """Plan, delegate, review, synthesise — the orchestration microservice."""

    def __init__(
        self,
        settings: OrchestratorSettings | None = None,
        *,
        models: Any = None,
        search: Any = None,
        agent_loop: Any = None,
        log_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.settings = settings if settings is not None else host.default_settings()
        self.models = models
        self.search = search
        # factory for the single-agent tool loop used by the `search` agent
        self.agent_loop = agent_loop or host.agent_loop()
        # where run records go; None = the host's log directory
        self.log_dir = log_dir

    # ------------------------------------------------------------- readiness
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "orchestrator_enabled", True))

    @property
    def roster(self) -> list[str]:
        return list(getattr(self.settings, "orchestrator_roster", []) or [])

    def describe(self) -> dict[str, Any]:
        """What this service can do — same shape the UI reads from ``GET /agents``."""
        return {
            **agent_catalogue(self.settings),
            "all_agents": catalogue(),
            "model_ready": bool(self.models),
            "search_ready": bool(self.search),
        }

    def boundary(self) -> dict[str, Any]:
        """Who is wired in on each port — for a host's /health, docs or a test."""
        return {
            "settings": type(self.settings).__name__,
            "models": type(self.models).__name__ if self.models is not None else "",
            "search": type(self.search).__name__ if self.search is not None else "",
            "agent_loop": getattr(self.agent_loop, "__name__", type(self.agent_loop).__name__),
            "log_dir": str(self.log_dir) if self.log_dir is not None else str(host.log_dir()),
        }

    # ------------------------------------------------------------------- run
    def manager(self) -> Manager:
        """The manager, wired with this service's collaborators."""
        return Manager(
            settings=self.settings,
            model_chain=self.models,
            search_chain=self.search,
            agent_loop=self.agent_loop,
            log_dir=self.log_dir,
        )

    async def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        """One turn: the whole pipeline, exactly as before — only the wiring moved."""
        if not isinstance(request, OrchestrationRequest):
            raise TypeError("run() takes an OrchestrationRequest (see app.orchestrator.contracts)")
        return await self.manager().run(
            request.message,
            attachments=list(request.attachments or []),
            history=list(request.history or []),
            agents=request.agents,
            planner=request.planner,
            synthesize=request.synthesize,
            review=request.review,
        )

    def payload(self, result: OrchestrationResult, *, trace_limit: Any = _DEFAULT) -> dict[str, Any]:
        """The JSON the browser gets: the trace clipped to ``ORCHESTRATOR_TRACE_CHARS``.

        Pass ``trace_limit=None`` for the unclipped record (what the run log keeps).
        """
        if trace_limit is _DEFAULT:
            trace_limit = getattr(self.settings, "orchestrator_trace_chars", 4000)
        return result.to_dict(trace_limit=trace_limit)
