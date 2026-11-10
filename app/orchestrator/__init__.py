"""The orchestrator — a self-contained service that sits beside the other services.

This package is the orchestration microservice: a manager, a planner, an
executor, four specialist agents, a trace and a run log. It talks to the rest of
the application only through **ports** it is handed at construction time
(``contracts.py``), and through a single host seam (``host.py``). Nothing inside
it imports the ARAS clients, the HTTP layer or the configuration module — so the directory can be lifted into another project with
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
  runbus.py      live channels: the trace, relayed to a watcher as it is written
  live.py        a trace event → one "thought" the page can group and show
  runlog.py      one JSON record per run (the orchestration log base)
  files.py       attachment decoding/extraction/store — the document agent's input
```

Sibling services (host side, not part of this package): ``app/aras.py`` (the
ARAS microservices — the router is the model, the web service is the search),
``app/agent.py`` (the single-agent tool loop), ``app/memory.py`` (conversations)
and ``app/main.py`` (the HTTP adapter that wires them together).

Typical host wiring — this is all ``app/main.py`` does::

    service = OrchestratorService(
        settings,
        models=aras.build_model(settings),      # ARAS router
        search=aras.build_search(settings),     # ARAS web
        agent_loop=WebSearchAgent,
    )
    result = await service.run(OrchestrationRequest(message=…, attachments=…))
    body = service.payload(result)
"""

from app.orchestrator.agents import AgentContext, AgentOutcome, catalogue
from app.orchestrator.audit import (
    LEVELS as AUDIT_LEVELS,
    catalogue as audit_catalogue,
    load_latest as load_audit,
    run_audit,
    scorecard_markdown as audit_markdown,
    write_scorecard as write_audit,
)

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
from app.orchestrator.live import plain_thought, thought
from app.orchestrator.manager import Manager, OrchestrationResult, agent_catalogue
from app.orchestrator.planner import AGENTS, Plan, PlanStep, heuristic_plan, make_plan
from app.orchestrator.runbus import BUS, END, Channel, RunBus
from app.orchestrator.service import OrchestratorService
from app.orchestrator.trace import RunTrace, new_run_id

__all__ = [
    "AGENTS",
    "AgentContext",
    "AgentLoop",
    "AgentLoopFactory",
    "AgentOutcome",
    "AUDIT_LEVELS",
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
    "BUS",
    "Channel",
    "END",
    "RunBus",
    "RunTrace",
    "SearchChain",
    "agent_catalogue",
    "audit_catalogue",
    "audit_markdown",
    "catalogue",
    "decode_payload",
    "extract",
    "heuristic_plan",
    "load_audit",
    "make_plan",
    "new_run_id",
    "plain_thought",
    "run_audit",
    "thought",
    "write_audit",
]
