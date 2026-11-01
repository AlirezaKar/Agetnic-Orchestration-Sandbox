"""The orchestrator — a self-contained service that sits beside the other services.

This package is the orchestration microservice: a manager, a planner, an
executor, four specialist agents, a trace and a run log. It talks to the rest of
the application only through **ports** it is handed at construction time
(``contracts.py``), and through a single host seam (``host.py``). Nothing inside
it imports the model client, the search stack, the HTTP layer or the
configuration module — so the directory can be lifted into another project with
a similar architecture and wired up in one place.

```
app/orchestrator/
  service.py     the entry point a host uses: OrchestratorService(...).run(request)
  contracts.py   ports (ModelChain, SearchChain, AgentLoopFactory, settings) + request
  host.py        the ONLY file that touches the host: logger, log dir, agent loop, settings
  manager.py     classify → plan → waves → review → synthesis
  planner.py     rules + LLM planner, per-agent briefs, dependency graph
  executor.py    parallel waves: gather + semaphore + timeout, failures isolated
  agents.py      document / search / data / general workers
  trace.py       every step of a run (prompts, tools, replies), clipping
  runlog.py      one JSON record per run (the orchestration log base)
  files.py       attachment decoding/extraction/store — the document agent's input
```

Sibling services (host side, not part of this package): ``app/llm`` (model
providers), ``app/search`` (web backends), ``app/agent.py`` (the single-agent
tool loop), ``app/memory.py`` (conversations) and ``app/main.py`` (the HTTP
adapter that wires them together).

Typical host wiring — this is all ``app/main.py`` does::

    service = OrchestratorService(
        settings,
        models=build_chain(settings),
        search=build_search_chain(settings),
        agent_loop=WebSearchAgent,
    )
    result = await service.run(OrchestrationRequest(message=…, attachments=…))
    body = service.payload(result)
"""

from app.orchestrator.agents import AgentContext, AgentOutcome, catalogue
from app.orchestrator.contracts import (
    AgentLoop,
    AgentLoopFactory,
    ModelChain,
    OrchestrationRequest,
    OrchestratorSettings,
    SearchChain,
)
from app.orchestrator.files import (
    Attachment,
    AttachmentStore,
    FileError,
    decode_payload,
    extract,
)
from app.orchestrator.manager import Manager, OrchestrationResult, agent_catalogue
from app.orchestrator.planner import AGENTS, Plan, PlanStep, heuristic_plan, make_plan
from app.orchestrator.service import OrchestratorService
from app.orchestrator.trace import RunTrace

__all__ = [
    "AGENTS",
    "AgentContext",
    "AgentLoop",
    "AgentLoopFactory",
    "AgentOutcome",
    "Attachment",
    "AttachmentStore",
    "FileError",
    "Manager",
    "ModelChain",
    "OrchestrationRequest",
    "OrchestrationResult",
    "OrchestratorService",
    "OrchestratorSettings",
    "Plan",
    "PlanStep",
    "RunTrace",
    "SearchChain",
    "agent_catalogue",
    "catalogue",
    "decode_payload",
    "extract",
    "heuristic_plan",
    "make_plan",
]
