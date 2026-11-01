# The orchestrator service

Everything in this folder is **one service**: a manager, a planner, an executor,
four specialist agents, a run trace, a run log and the attachment handling the
document agent needs. It sits beside the other services of the app (`app/llm`,
`app/search`, `app/agent.py`, `app/memory.py`) and knows nothing about them —
only about the ports it is handed.

The point of the shape: **copy this folder into another project with a similar
architecture, rewrite `host.py`, wire `OrchestratorService` once.** No algorithm
lives in this file set that would need to change.

```
app/orchestrator/
  service.py     OrchestratorService — the single entry point
  contracts.py   ports: ModelChain · SearchChain · AgentLoopFactory · settings · request
  host.py        the ONLY file that imports the host application
  manager.py     classify → plan → waves → review → synthesis
  planner.py     rules + LLM planner, per-agent briefs, dependency graph
  executor.py    parallel waves (gather + semaphore + timeout), failures isolated
  agents.py      document / search / data / general workers
  trace.py       every step of a run (prompts, tool calls, replies), clipping
  runlog.py      one JSON record per run — the orchestration log base
  files.py       attachment decoding / extraction / store (the document agent's input)
```

## What the service needs (the four ports)

| Port | Signature it must satisfy | This project's implementation |
|---|---|---|
| `ModelChain` | `await complete(messages, tools=None, temperature=…, max_tokens=…)` | `app.llm.registry.build_chain(settings)` |
| `SearchChain` | `await search(query, limit)` | `app.search.registry.build_search_chain(settings)` |
| `AgentLoopFactory` | `factory(model_chain, search_chain, settings, observer=hook)` → object with `await run(message, history=…, allow_search=…, max_steps=…)` | `app.agent.WebSearchAgent` |
| `OrchestratorSettings` | attributes listed in `contracts.py` (15 plain values) | `app.config.Settings` |

The `observer` hook is how the single-agent loop reports its prompts, model
replies and tool calls into the run trace — the same contract the web UI shows.

## Wiring it (exactly what `app/main.py` does)

```python
from app.orchestrator import OrchestrationRequest, OrchestratorService

service = OrchestratorService(
    settings,                                  # any object shaped like OrchestratorSettings
    models=build_chain(settings),
    search=build_search_chain(settings),
    agent_loop=WebSearchAgent,                 # optional — defaults to host.agent_loop()
    log_dir=None,                              # optional — defaults to the host's log dir
)

result = await service.run(OrchestrationRequest(
    message="Compare the refund policy with the CSV totals.",
    history=[…],                               # earlier turns, if you keep them
    attachments=[attachment],
    planner="auto", review=True, synthesize=True, agents=None,
))

body = service.payload(result)                 # JSON for the browser (trace clipped)
full = service.payload(result, trace_limit=None)   # unclipped, same as the run log
service.describe()                             # GET /agents payload
```

## Porting checklist

1. Copy `app/orchestrator/` into the new project (keep the folder name; imports are
   `app.orchestrator.*` — adjust the package path if the new project names it differently).
2. Rewrite **`host.py`** (four small functions): your logger, your log directory,
   your single-agent tool loop, your settings factory.
3. Implement or reuse the three ports. If your model client returns a different
   response object, the only requirement is `content`, `tool_calls`, `usage`,
   `provider`, `model` (see `trace.add_response`).
4. Wire one `OrchestratorService` where your HTTP/adapter layer is.
5. `python -m pytest tests/test_orchestrator_service.py` — the boundary test walks
   the AST of this folder and fails if anything imports the host outside `host.py`.

## Boundaries that are enforced

`tests/test_orchestrator_service.py` asserts, by parsing the source of every file
in this folder, that:

- nothing imports `app.llm`, `app.search`, `app.agent`, `app.memory`, `app.main`
  or `app.config` — those are sibling services reached through ports;
- only `host.py` imports anything from the host at all;
- `contracts.py` imports nothing but the standard library;
- no module hides an import behind `importlib` / `__import__`.

## Deliberately *not* moved

The logs themselves (`app/logs.py`, the file handlers, the redaction, the
`agetnic.*` logger names) stay where they were: the orchestrator only asks the
host for a logger and a directory. Same for the HTTP layer, the configuration
loader and the conversation memory — they are host concerns, wired in `app/main.py`.
