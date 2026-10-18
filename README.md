# Agetnic Orchestration Sandbox

A **Manager-rooted multi-agent orchestration** sandbox for web apps — with a
plain HTML/CSS/JS test harness whose only job is to exercise the orchestrator
and expose everything it does.

> **What this is:** the orchestration *core* (backend) + a test UI.  The
> backend is designed to be lifted into a real web app; the frontend is a
> debugging/evaluation console, not a product.

---

## Architecture

```
                         ┌──────────────────────────────────────────────┐
   user prompt ──────────▶                   MANAGER                    │
   (any wording,         │  1. analyse the goal          (thinking)     │
    any mode)            │  2. plan via Planner          (task plan)    │
                         │  3. delegate to agents        (parallel)     │
                         │  4. supervise / re-plan       (on failure)   │
                         │  5. synthesise final answer   (synthesis)    │
                         └───────┬──────────────┬───────────┬───────────┘
                                 │              │           │
                     ┌───────────▼──┐   ┌───────▼─────┐   ┌──▼───────────┐
                     │   PLANNER    │   │  EXECUTOR   │   │    TRACES    │
                     │ goal → tasks │   │ wave-based  │   │ traces/*.json│
                     │ + depends_on │   │ parallelism │   │ input/…/out  │
                     └──────────────┘   └──────┬──────┘   └──────────────┘
                                    ┌──────────┼──────────┐
                          ┌─────────▼──┐ ┌─────▼────┐ ┌───▼───────┐ ┌─────▼────┐
                          │  DOCUMENT  │ │ WEB SEARCH│ │  GENERAL  │ │  NUMBER  │
                          │ RAG over   │ │ search +  │ │ chat /    │ │ data &   │
                          │ uploads    │ │ grounding│ │ knowledge │ │ analysis │
                          └────────────┘ └──────────┘ └───────────┘ └──────────┘
```

### Modes

| Mode | Behaviour |
|------|-----------|
| **auto** | The **Manager** analyses the goal, the **Planner** decomposes it into dependency-aware tasks, and the **parallel Executor** runs them (independent tasks concurrently, dependent tasks in waves). Results are **synthesised** into one answer. Failing tasks trigger a bounded **re-plan**. |
| **document** | Hand-off to the Document agent — RAG over uploaded `.txt`/`.md` files (local TF-IDF retrieval). |
| **web_search** | Hand-off to the Web-search agent — pluggable search provider + grounded summarisation. |
| **general** | Hand-off to the General agent — conversation & general knowledge. |
| **number** | Hand-off to the Number agent — quantitative analysis, with a *safe* local arithmetic tool for exact math. |

The classic "hand-off router" is kept as first-class fallback: if planning
fails, the Router keyword-routes the goal to the single best agent.

## Repository layout

```
backend/
  config.py            organised .env-driven settings (single source of truth)
  schemas.py           shared Pydantic models (plans, results, traces, HTTP)
  llm/                 LLM abstraction: Groq (real) + deterministic offline Mock
  agents/              worker agents: document · web_search · general · number
  orchestration/       Manager · Planner · parallel Executor · Router · Traces · Sessions
  rag/                 local document store + TF-IDF retrieval
  search/              search providers: simulated (default) · tavily · serper
  api/                 FastAPI app + REST endpoints
frontend/              HTML/CSS/JS test harness (no build step)
tests/                 pytest suite (runs fully offline on the mock LLM)
scripts/check.py       the final quality gate
traces/                per-run JSON debug records (gitignored, auto-created)
data/documents/        uploaded RAG corpus (gitignored, auto-created)
```

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure (optional — works with zero keys thanks to the offline mock)
cp .env.example .env           # then add GROQ_API_KEY for real LLM calls

# 3. Run the test harness
python3 run_server.py          # → http://localhost:8000/
```

With `GROQ_API_KEY` set, the Manager/Planner/agents use **Groq**
(`llama-3.3-70b-versatile` by default — free tier).  Without a key, a
deterministic **mock LLM** keeps the whole pipeline runnable and testable.

## Configuration (`.env`)

Every knob lives in [`.env.example`](.env.example), organised in five
sections: **LLM/Orchestrator**, **Web search**, **Orchestration**,
**Storage**, **Server**.  Highlights:

| Variable | Meaning | Default |
|----------|---------|---------|
| `LLM_PROVIDER` | `auto` (Groq if key, else mock) · `groq` · `mock` | `auto` |
| `GROQ_API_KEY` | Orchestrator + agent API key | *(empty)* |
| `GROQ_MODEL_ORCHESTRATOR` | Model for Manager & Planner | `llama-3.3-70b-versatile` |
| `GROQ_MODEL_AGENT` | Model for worker agents | `llama-3.3-70b-versatile` |
| `WEB_SEARCH_PROVIDER` | `simulated` · `tavily` · `serper` | `simulated` |
| `MAX_PARALLEL_TASKS` | Concurrent agent tasks per wave | `4` |
| `MAX_REPLAN_ATTEMPTS` | Manager re-plans after failure | `1` |

## Evaluation & debugging — JSON traces

Every run writes `traces/<run_id>.json` containing **everything**:

```jsonc
{
  "run_id": "run_fa6155cc",
  "mode_requested": "auto", "mode_used": "auto",
  "input": "…the raw user prompt…",
  "thinking": ["…Manager analysis…", "…Planner reasoning…"],
  "plan": { "tasks": [ {"id":"t1","agent":"web_search","depends_on":[]}, … ] },
  "steps": [
    { "type": "plan",        "thinking": "…", "data": {"waves": [["t1"],["t2"]]} },
    { "type": "agent_task",  "input": "…", "output": "…", "thinking": "…",
      "duration_ms": 12.3, "data": {"agent": "web_search", "wave": 0} },
    { "type": "synthesis",   "output": "…final answer…" }
  ],
  "output": "…final answer…",
  "metrics": { "tasks_completed": 2, "agents_used": ["number","web_search"] },
  "config": { "llm_provider": "groq", "…redacted…": "…" }
}
```

`traces/index.jsonl` gives one summary line per run.  The UI's **Trace
inspector** shows the same JSON live.

## Tests

```bash
python3 scripts/check.py       # the final quality gate (pytest, fully offline)
```

Coverage: config & secret redaction · mock/Groq LLM factory · planner
parsing & fallback · parallel wave execution & failure isolation ·
Manager flow (auto, manual, re-planning) · agent behaviour (safe math,
search grounding, RAG) · router heuristics · trace persistence · HTTP API.

## Extending

* **New specialist agent** → implement `BaseAgent`, register it in
  `backend/agents/registry.py`, add an `AgentName` enum value.  The Planner
  prompt lists it and the Manager can delegate to it immediately.
* **Real web search** → set `WEB_SEARCH_PROVIDER=tavily` (or `serper`) and
  the matching API key; no code changes.
* **Other LLM providers** → implement `BaseLLMClient` (one method) and add
  a case in `backend/llm/factory.py`.
