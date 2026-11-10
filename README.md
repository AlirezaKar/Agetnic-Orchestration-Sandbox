# Agetnic Orchestration Sandbox

A manager-led, multi-agent orchestrator with a studio page to watch it work,
running on the **ARAS microservices** — and on nothing else.

The repository is three parts:

| Part | Where | What it is |
|---|---|---|
| **The orchestrator** | `app/orchestrator/` | a manager plans the request, specialist agents (document · web search · data · general) run in parallel waves, an evaluator scores and tightens every report, the manager writes one answer |
| **The UI** | `app/static/` | the studio page: chat, live thoughts, run inspector, self-audit, service status — one static page, no build step |
| **ARAS** | `app/aras.py` | the only backend: the router is the model, the web service is the search, OCR reads the PDFs, retrieval and ingestion are the knowledge store, the data service answers analytics |

Everything else in `app/` is the plumbing that holds those three together: the
HTTP routes (`app/main.py`), the settings (`app/config.py`), the single-agent
tool loop the search agent runs (`app/agent.py`), the conversation memory
(`app/memory.py`) and the logs (`app/logs.py`).

- **One model, one search, no fallback.** Every model call is the ARAS router;
  every web search is the ARAS web service. There is no second provider to
  switch to and no mock: when a service is not configured or does not answer,
  the call fails with the service's name, the HTTP status and the env vars or
  the fix — never with a canned answer.
- **You can watch it think.** The page streams the run while it happens
  (`POST /orchestrate/stream`, server-sent events): a group per actor — planner,
  each agent, evaluator, manager — with a spinner on whoever is working and
  every line opening to the prompt, reply, tool call or verdict behind it.
- **You can verify every layer**: `python scripts/aras_smoke.py` (tunnel,
  health, tokens — without the app), `GET /aras` (health of every service),
  `GET /diagnose` (a real model call, a real search, the next thing to fix),
  and the offline test suite.
- **You can evaluate the orchestration itself**: the **Evaluate** button (or
  `POST /audit/run`) runs eighteen checks over the real pipeline, offline, in
  about a third of a second, and hands back one scorecard (§5).

---

## 1. Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# open .env and fill the ARAS block — three lines are enough for a first answer:
#   ARAS_BASE_URL=http://127.0.0.1          (the SSH tunnel, see §2)
#   ARAS_TOKEN=...                          (or one token per service)
#   ARAS_MODEL=sharif-rayen-gemma4-31b      (an id from GET /aras/models)

python scripts/aras_smoke.py      # is the tunnel up, does each service answer, do the tokens work?
python run.py                     # the page and the API on http://localhost:8000
```

`run.py` is the entry point: **one** process serves the page and the API. On
start it prints the model and the search it is wired to, names every ARAS
variable that is still missing, and starts the logs described in §10. The server
starts even with an empty ARAS block — the page loads, and every status pill
says what to set. Plain `uvicorn app.main:app --port 8000` works too.

```bash
python run.py --reload                    # auto-reload while editing
python run.py --port 8080 --log-level DEBUG
```

Open <http://localhost:8000> — the page is described in §6. Then:

```bash
curl -s localhost:8000/diagnose | python -m json.tool     # model + search + health + hints
python -m unittest discover -s tests                      # offline: no ARAS, no network
```

---

## 2. ARAS microservices

The orchestrator needs a model, a web search, a PDF reader, a knowledge store
and an analytics backend. Each is one ARAS service, and each fills one port of
the orchestrator (§5). `app/aras.py` is the only module that knows their URLs,
their auth and their JSON; it is also the only module in `app/` that opens a
network connection.

| Service | Port | Its job here | Route used | Needs |
|---|---|---|---|---|
| **router** | 8101 | **the model** — every planner, agent, evaluator and manager call | `POST /api/v1/chat/completions/` | `ARAS_ROUTER_TOKEN`, `ARAS_MODEL` |
| **web** | 8108 | **web search** — the `web_search` tool of the search agent | `POST /v1/search/` | `ARAS_WEB_TOKEN` |
| ocr | 8102 | reads uploaded PDFs into text for the document agent | `POST /api/v1/ocr/` | `ARAS_OCR_TOKEN` |
| retrieval | 8105 | the document agent's knowledge store (RAG) when no file is attached | `POST /v1/search/` | `ARAS_RETRIEVAL_TOKEN`, `ARAS_COLLECTION` |
| ingestion | 8104 | indexes accepted uploads into the collection | `POST /v1/ingest/` | `ARAS_INGESTION_TOKEN`, `ARAS_COLLECTION`, `ARAS_AUTO_INGEST=true` |
| data | 8107 | the data agent's analytics backend when there is nothing local to compute | `POST /v1/query/` | `ARAS_DATA_TOKEN`, `ARAS_TENANT`, `ARAS_OWNER`, `ARAS_DATA_SOURCE_ID` |
| vector | 8103 | health and diagnostics only — ingestion and retrieval are the working doors | `GET /v1/collections/` | `ARAS_VECTOR_TOKEN` |

Only the router is required: it is the model, and without it there is no
written answer — only the figures, excerpts and search results the agents can
collect on their own. Every other service is optional and switches one
capability on. Port 8106 (the remote orchestrator) is never called — this app
*is* the orchestrator.

### Reaching the services

They listen on the ARAS server's loopback, so open an SSH tunnel from the
machine that runs this app and keep `127.0.0.1` addressing:

```bash
ssh -N -o ServerAliveInterval=30 \
    -L 8101:127.0.0.1:8101 -L 8102:127.0.0.1:8102 -L 8103:127.0.0.1:8103 \
    -L 8104:127.0.0.1:8104 -L 8105:127.0.0.1:8105 -L 8107:127.0.0.1:8107 \
    -L 8108:127.0.0.1:8108 <user>@<aras-server>
```

On Windows the command is the same, written on one line.

### Settings

```dotenv
ARAS_BASE_URL=http://127.0.0.1        # one host root; the service ports are appended
ARAS_TOKEN=...                        # shared token: the fallback for every service token
ARAS_ROUTER_TOKEN=...                 # ...or one token per service
ARAS_WEB_TOKEN=...
ARAS_MODEL=sharif-rayen-gemma4-31b    # the id the router is asked for
ARAS_COLLECTION=alireza_notes         # the RAG collection (retrieval + ingestion)
ARAS_TENANT=...                       # the data service's identity headers
ARAS_OWNER=...
ARAS_DATA_SOURCE_ID=...               # the source POST /v1/query/ answers from
```

| Variable | Default | Meaning |
|---|---|---|
| `ARAS_BASE_URL` | *(empty)* | host root, e.g. `http://127.0.0.1`; each service is `base:port` |
| `ARAS_TOKEN` | *(empty)* | used for any service whose own token is not set |
| `ARAS_ROUTER_TOKEN` `ARAS_WEB_TOKEN` `ARAS_OCR_TOKEN` `ARAS_RETRIEVAL_TOKEN` `ARAS_INGESTION_TOKEN` `ARAS_DATA_TOKEN` `ARAS_VECTOR_TOKEN` | `ARAS_TOKEN` | one bearer token per service |
| `ARAS_MODEL` | *(empty)* | **required.** The model id sent to the router. `GET /aras/models` lists the ids it serves |
| `ARAS_TIMEOUT` | `60` | seconds per ARAS call |
| `ARAS_COLLECTION` | *(empty)* | the collection retrieval searches and ingestion writes; empty = no retrieval |
| `ARAS_RETRIEVAL_MODE` | `hybrid` | `semantic`, `keyword` or `hybrid` |
| `ARAS_AUTO_INGEST` | `false` | also index every accepted upload into `ARAS_COLLECTION` |
| `ARAS_TENANT` `ARAS_OWNER` | *(empty)* | sent as `X-Tenant` / `X-Owner-External-Id` to the data service |
| `ARAS_DATA_SOURCE_ID` | *(empty)* | the analytics source; empty = the data agent does not ask the service |
| `ARAS_ROUTER_URL` `ARAS_WEB_URL` `ARAS_OCR_URL` `ARAS_RETRIEVAL_URL` `ARAS_INGESTION_URL` `ARAS_DATA_URL` `ARAS_VECTOR_URL` | `base:port` | full URL of one service when it is not `ARAS_BASE_URL` + its port |
| `LLM_TEMPERATURE` `LLM_MAX_TOKENS` | `0.2` `1024` | sent with every router call |

### The rules

- **The model you write is the model that is called.** `ARAS_MODEL` is never
  guessed, substituted or picked from a list. If it is empty the router counts
  as not configured and the error says so. The router answers under the model's
  own name — `sharif-rayen-gemma4-31b` comes back as `rayen-gemma4-31b` — and
  that is the name the run record shows.
- **A service is off until it has a URL and a token.** `GET /providers` lists
  each one as `ready`, `incomplete` (URL + token are there, another value is
  not), `off` (ingestion with `ARAS_AUTO_INGEST=false`) or `not configured`,
  with the exact variables still missing.
- **Nothing answers in a service's place.** A router failure is a `502` that
  names the router; a web-search failure is a `502` that names the web service.
  In Auto mode the agent that needed the service reports `failed` with the
  reason and the rest of the run continues (§5).
- **Local work stays first.** The data agent computes attached tables and plain
  arithmetic itself; the data service is asked only when the request brings
  nothing to compute. The document agent reads the attached file; the knowledge
  store is its source only when there is none.
- **Retrieval is reached through the model planner.** The planner's rules hand
  a request to the document agent only when a file is attached. A question
  about the indexed collection therefore reaches retrieval when the *model*
  plans the run and picks the document agent: `"planner": "llm"` on one
  request, or `ORCHESTRATOR_PLANNER=llm` for all of them (§5).
- **Loopback never goes through a proxy.** Calls to `127.0.0.1` ignore
  `HTTP_PROXY`/`HTTPS_PROXY`, so a VPN's proxy cannot answer for the tunnel.
- **Tokens are only ever sent to their own service.** A token travels in the
  `Authorization` header of a call to that service and nowhere else: errors
  carry the service, the status and the service's own message, no route returns
  a token, and `GET /providers` lists variable *names*, never values.

---

## 3. Verifying it works

Four layers, cheapest first. Each failure points at a different fix.

### Layer 1 — the tunnel and the tokens, without the app

```bash
python scripts/aras_smoke.py            # reads .env next to you; --env PATH, --json
```

Standard library only, no project imports — copy the file anywhere. Per
service it tries a TCP connect (closed → the tunnel is not forwarding), the
unauthenticated health route (non-200 → something else is answering) and one
read-only authenticated call (401/403 → the token is wrong):

```text
OK   router     :8101  port=open  health=200  auth=200
SKIP ocr        :8102  port=open  health=200  auth=-
     not configured: no token in .env (ARAS_OCR_TOKEN or ARAS_TOKEN) - skipped
FAIL web        :8108  port=open  health=200  auth=401
     TOKEN REJECTED (check the token in .env; key inactive?)
```

A service with no token is `SKIP`: it is shown and decides nothing, so leaving
an optional service out is not a failure. Exit code 0 only when at least one
service has a token and every one that has passes all three. The script probes
the standard ports on the host of `ARAS_BASE_URL`; it does not follow a
per-service `ARAS_<SERVICE>_URL`.

### Layer 2 — HTTP, with the server running

```bash
curl -s localhost:8000/providers   | python -m json.tool   # config only: state + missing vars per service
curl -s localhost:8000/aras        | python -m json.tool   # live: one health probe per configured service
curl -s localhost:8000/aras/models | python -m json.tool   # the model ids the router serves
curl -s localhost:8000/diagnose    | python -m json.tool   # a real completion, a real search, every health probe
```

`GET /diagnose` is the one-stop check. Its `hints` list is ordered from the most
fundamental problem down — no `.env`, a missing variable, a closed tunnel, a
proxy in the way, a model id the router refused:

```json
{
  "model":  {"ok": true, "provider": "aras", "model": "rayen-gemma4-31b", "reply": "pong", "latency_ms": 412},
  "search": {"checked": [{"provider": "aras", "ok": true, "count": 3, "latency_ms": 310, "sample": ["…"], "empty": false}],
             "skipped": [], "ok": ["aras"], "failed": []},
  "aras":   {"configured": ["router", "retrieval", "web"],
             "checked": [{"service": "router", "ok": true, "configured": true, "latency_ms": 9}, "…"],
             "model_ready": true, "search_ready": true, "rag_ready": false, "data_ready": false,
             "missing": {"retrieval": ["ARAS_COLLECTION"], "…": ["…"]}},
  "hints":  ["ARAS retrieval is configured but ARAS_COLLECTION is empty, so the document agent cannot retrieve — name the collection (e.g. alireza_notes)."]
}
```

A failed check carries the same `error` object as an API error (§7): the
service, the HTTP status, the service's own message and a hint.

### Layer 3 — in the browser

- **Status ▾** shows the `MODEL`, `SEARCH`, `ARAS`, `Diag` and `Audit` pills;
  the dot on the button is the worst of them. Hover a red pill for the
  variables to set.
- **Tools ▾ → ARAS services** lists every service with its role, endpoint,
  state and what it still needs (`GET /providers`).
- **Tools ▾ → ARAS wiring** runs `GET /aras`, and **Proof run** sends one tiny
  real request through `POST /orchestrate`: the run record names the model the
  router answered with.
- **Tools ▾ → Run diagnostics** puts the `GET /diagnose` report in the
  inspector, one row per check.

### Layer 4 — the code, offline

```bash
python -m unittest discover -s tests        # or: python -m pytest tests -q
node tests/ui_smoke.js                      # the page in jsdom (npm i jsdom once); add a base URL for a live server
node tests/ui_audit.js && node tests/ui_consent.js
python -m scripts.audit                     # the orchestration's own eighteen checks (§5)
```

The Python suite needs no ARAS service: `app/aras.py` is driven through
`httpx.MockTransport`, so URLs, headers, bodies and error mapping are checked
against recorded shapes. `tests/test_docs.py` guards the strip-down: the removed
packages, scripts and settings stay gone, nothing shipped names one of them,
`app/aras.py` stays the only HTTP client, and the page, `.env.example` and this
file name no other vendor.

---

## 4. One agent, one tool: `POST /chat` and `POST /search`

`app/agent.py` is the single-agent loop: the model, and one tool it may call.

```
user message ─► model ─► tool call web_search(query) ─► ARAS web ─► results ─► model ─► answer + sources
                  ▲                                                              │
                  └───────────────── up to AGENT_MAX_STEPS rounds ───────────────┘
```

```bash
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"What is the newest Python release? Cite sources.","max_steps":4}' \
  | python -m json.tool
```

The reply carries the `answer`, the `sources` it used, the `model` that
answered, the number of `steps` and a `trace` with every model turn and tool
call. `allow_search: false` removes the tool (the page's **Answer** mode).
A failed search is handed back to the model as a tool error, so the answer can
say truthfully that the search failed instead of inventing results; the reply
lists it under `errors` with the service's message and hint.

`POST /search {"query": "...", "max_results": 5}` runs the web service directly —
no model, no loop (the page's **Search** mode).

The same loop is the search agent's engine inside Auto mode: the orchestrator
receives it as a port (`AgentLoopFactory`) and never imports it.

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_MAX_STEPS` | `4` | model rounds before the loop must answer |
| `AGENT_MAX_SEARCH_RESULTS` | `5` | hits handed to the model per search |

---

## 5. Manager orchestration (Auto mode)

The single-agent loop is one model with one tool. Auto mode adds a lead:

```
1. classify      rules read the prompt, the files and the roster
2. plan          rules select the agents ─► the model writes each agent's brief + who waits for whom
3. delegate      wave 1: independent agents run in parallel
                 wave 2: an agent that depends on another receives its result as input
4. evaluate      the evaluator scores every report, names the gaps and rewrites the weak ones
5. review        the manager reads the *evaluated* reports and may ask for another wave
                 (that wave goes back through the evaluator before anyone reads it)
6. wrap up       the manager writes one answer from all reports (or merges them deterministically)
```

```
                     ┌── Document analyst ── attachments, quotes ──┐
prompt ─► MANAGER ─► PLANNER ─┼── Web researcher ──── URLs, sources ─┼─► EVALUATOR ─► MANAGER ─► answer
                     ├── Data analyst ────── exact figures ──────┤   score +        review     (synthesis)
                     └── Generalist ──────── own knowledge ──────┘   rewrite        + new wave?
                          wave 1 in parallel → wave 2 uses wave 1        ▲             │
                                                                        └── the new wave ┘
```

The evaluator is the reason the review is worth having: it is one thing to see
forty lines of agent output, and another to read "the web report has no date,
the data report dropped its figures" and decide about *that*. Its rewrite only
reorganises what the agent already wrote — a rewrite that loses a URL or a
figure the original had is discarded (see `keep_facts` in
`app/orchestrator/evaluator.py`). Every report keeps both versions in the run
record: `agents[i].answer` (what the agent said) and `agents[i].optimized`
(what the manager read).

| Piece | Where | What it decides |
|---|---|---|
| **Service** | `app/orchestrator/service.py` | the entry point and the wiring: `OrchestratorService(settings, models=…, search=…, agent_loop=…)` |
| **Ports** | `app/orchestrator/contracts.py` | what the orchestrator needs from outside (model, search, agent loop, knowledge, data, settings) — protocols, not imports |
| **Host seam** | `app/orchestrator/host.py` | the only file in the package that imports the app (logger, log dir, agent loop, settings) |
| Manager | `app/orchestrator/manager.py` | classify → plan → waves → review → one answer (+ sources, errors, timings) |
| Planner | `app/orchestrator/planner.py` | which agents run, the brief each one gets, and the dependency graph |
| Executor | `app/orchestrator/executor.py` | waves: `asyncio.gather` + semaphore + per-agent timeout; failures stay isolated |
| Evaluator | `app/orchestrator/evaluator.py` | scores each report (verdict, 1–5, issues), names the gaps, rewrites the weak ones; rules-only when there is no model |
| Agents | `app/orchestrator/agents.py` | document / search / data / general, one signature, one result type |
| Trace | `app/orchestrator/trace.py` | every step of the run: prompts, tool calls, tool results, replies |
| Live | `app/orchestrator/live.py` | the trace projected into "thoughts" for the stream |
| **Self-audit** | `app/orchestrator/audit.py` | eighteen checks over the parts above, through scripted ports: one scorecard, no service, no network |
| Run log | `app/orchestrator/runlog.py` | one unclipped JSON record per run in `logs/orchestrator/` |
| Attachments | `app/orchestrator/files.py` | upload decoding, extraction, on-disk store |
| Memory | `app/memory.py` | the conversation itself: every user turn + answer in `memory/`, replayable |

### The orchestrator is a service, ARAS fills its ports

The orchestrator **imports none of its neighbours**. It is handed ports and one
host seam, and this is the whole wiring — the only place the orchestrator and
ARAS meet:

| Port | Shape | This app passes |
|---|---|---|
| `ModelChain` | `await complete(messages, tools=None, temperature=…, max_tokens=…)` | `aras.build_model(settings)` — router :8101 |
| `SearchChain` | `await search(query, limit)` | `aras.build_search(settings)` — web :8108 |
| `AgentLoopFactory` | `factory(model_chain, search_chain, settings, observer=hook)` | `app.agent.WebSearchAgent` |
| `KnowledgeStore` *(optional)* | `await retrieve(query, limit)` | `aras.build_knowledge(settings)` — retrieval :8105 |
| `DataBackend` *(optional)* | `await query(question)` | `aras.build_data_backend(settings)` — data :8107 |
| `OrchestratorSettings` | plain attributes (`contracts.py`) | `app.config.Settings` |

```python
# app/main.py — the whole host side of the orchestration
service = OrchestratorService(
    settings,
    models=aras.build_model(settings),          # ARAS router
    search=aras.build_search(settings),         # ARAS web
    agent_loop=WebSearchAgent,
    knowledge=aras.build_knowledge(settings),   # ARAS retrieval, or None
    data_backend=aras.build_data_backend(settings),
)
result = await service.run(OrchestrationRequest(message=…, attachments=…))
body   = service.payload(result)      # trace clipped for the browser
```

A port that is not ready is **falsy**: that is how an agent knows to report
"no model is configured" instead of calling one. The orchestrator never asks
*which* backend is behind a port.

That boundary is enforced, not just described.
`tests/test_orchestrator_service.py` parses the AST of every file in the
package and fails if anything imports the host outside `host.py`; it also runs
the package against *foreign* objects (a dataclass settings object, a
two-method model, an injected agent loop). `app/orchestrator/README.md` is the
checklist for lifting the folder into another project.

### What a plan looks like

```bash
curl -s -X POST localhost:8000/orchestrate -H 'content-type: application/json' \
  -d '{"message":"Explain the refund rule in the attached policy.","file_ids":["<id from POST /files>"]}' \
  | python -m json.tool
```

```json
{
  "answer": "…the manager's written answer…",
  "mode": "auto",
  "provider": "aras", "model": "rayen-gemma4-31b",
  "run_id": "run-20261031-153930-fc0f41",
  "orchestration": {
    "run_id": "run-20261031-153930-fc0f41",
    "strategy": "auto", "synthesized": true, "model_calls": 5,
    "parallel_ms": 50, "latency_ms": 134,
    "plan": {
      "intent": "document-qa", "detail_source": "llm",
      "summary": "Read the policy file first, then explain the refund rule in plain words.",
      "steps": [
        {"agent": "document", "task": "Extract the refund window…", "depends_on": [], "wave": 1},
        {"agent": "general", "task": "Explain the extracted rule…", "depends_on": ["document"], "wave": 2}
      ],
      "agents": ["document", "general"], "waves": [["document"], ["general"]], "parallel": true
    },
    "waves": [{"wave": 1, "agents": ["document"]},
              {"wave": 2, "agents": ["general"], "depends_on": {"general": ["document"]}}],
    "evaluation": {
      "requested": true, "ran": true, "method": "model", "rounds": 1,
      "summary": "the file report buries the rule", "gaps": ["no effective date"],
      "quality": 4.0, "revised": 1, "failed": 0, "model_calls": 1,
      "items": [{"agent": "document", "label": "Document analyst", "verdict": "revise",
                 "score": 4, "issues": ["buries the rule"], "revised": true, "usable": true}]
    },
    "review": {"asked": true, "rounds": 1, "note": "the file answer is enough", "follow_up": []},
    "trace": [
      {"seq": 2, "stage": "planner", "actor": "planner", "title": "Selected the agents by rule", "detail": {"selected": ["…"]}},
      {"seq": 3, "stage": "planner", "actor": "planner", "title": "Planner prompt (writes each agent's brief)",
       "detail": {"messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}]}},
      {"seq": 11, "stage": "agent", "actor": "document", "title": "Prompt sent to the model",
       "detail": {"messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}]}},
      {"seq": 24, "stage": "synthesis", "actor": "manager", "title": "Final synthesis prompt", "detail": {"messages": ["…"]}}
    ],
    "log_path": "logs/orchestrator/run-20261031-153930-fc0f41.json"
  },
  "sources": []
}
```

### Who writes what

| Stage | Default | Knob |
|---|---|---|
| selection | rules over wording, attachments, numbers — free, ~0 ms | `ORCHESTRATOR_PLANNER=llm` lets the model choose instead |
| briefs | **the model** writes one task per selected agent + the dependencies (`auto`) | `ORCHESTRATOR_PLANNER=heuristic` = rules write the briefs, no planner call at all |
| agents | one call each, in parallel **waves** (wall clock ≈ the slowest) | `ORCHESTRATOR_MAX_PARALLEL`, `ORCHESTRATOR_TIMEOUT` |
| **evaluate** | one small call over all reports, before the manager reads them | `ORCHESTRATOR_EVALUATE=false`; `ORCHESTRATOR_OPTIMIZE=false` keeps the scoring but discards the rewrites |
| review | one small call, only when more than one agent ran | `ORCHESTRATOR_REVIEW=false`, `ORCHESTRATOR_REVIEW_ROUNDS=n` |
| wrap-up | one synthesis call | `ORCHESTRATOR_SYNTHESIZE=false` → deterministic labelled merge |

So a two-agent run costs six router calls — the planner (with `auto`), two
agents, the evaluator, the review and the synthesis — plus one for every search
the search agent runs (its loop asks the model again after each tool result),
and another wave when the review asks for one. Every call is visible in the
trace, and `orchestration.model_calls` counts them all.

**Dependencies.** The planner may say "the generalist waits for the document
analyst". The executor then runs the generalist in wave 2, and hands it the
document analyst's answer as input:

```
Findings from agents that ran before you. Use them where they help, do not repeat them,
and do not contradict them without saying why:

### Document analyst (document) — ok
policy.md, refund section: refunds are allowed within 30 days of purchase.
```

A cycle or a dependency on an agent that never ran cannot deadlock the run:
those steps drop into the next wave with no input.

Rules of thumb in the heuristic planner:

- any attachment → **document** (and only then — see "Retrieval" in §2); a tabular attachment, analysis wording, or ≥3 numbers → **data**
- freshness/source wording ("latest", "news", "price", "cite", "source") → **search**
- nothing applies, or the request asks to explain/compare/design → **general**
- specialists present *and* reasoning asked for → generalist joins; a plain lookup stays one agent
- `ORCHESTRATOR_AGENTS` is the roster; an agent missing from it is never planned — except the generalist, which takes the request when nothing in the roster applies

What each agent uses:

| Agent | Works from | ARAS service behind it |
|---|---|---|
| document | the attached files; with none attached, the knowledge store | ocr :8102 (PDFs), retrieval :8105, ingestion :8104 |
| search | the single-agent loop of §4 | web :8108 |
| data | attached tables and arithmetic, computed locally; otherwise the analytics backend | data :8107 |
| general | the model alone | — |

All four write their report with the router. A run still returns when the
router is missing: the document agent returns the excerpts it found, the data
agent the figures it computed, the search agent the raw results, the generalist
reports that it has no model, and the manager merges what there is with a note
that no model was available — a trace and a reason, never a 500. `POST /chat`
has nothing to merge, so it fails with `400` and the missing variables.

A service that fails during a run fails the agent that needed it, not the
request: the agent reports `failed` with the service's own message
(`[aras:web] 401 unauthorized — the token was rejected`) and a hint, it is
listed under `errors`, and the manager writes the answer from the reports that
are left. For the search agent that means it tried to search and no search
came back — what the model wrote without results is dropped, not passed on as
research. One failed call followed by one that works stays `ok`, and the
report's `detail` counts the failed call. When the router itself fails, every
agent that needed the model fails with the router's message and hint.

### Seeing every step: the trace and the run log

The UI renders the whole run under the answer ("How this answer was built"):
one row per step, each expandable — the manager's classification, the planner
prompt and raw reply, the briefs it wrote, the wave the agents started in, each
agent's prompt (system + user, verbatim), every tool call with its arguments and
result, each agent's reply, the manager review, and the final synthesis prompt.
**Expand all steps** opens everything at once; **Raw JSON** sends the payload to
the inspector.

The same data is on the wire and on disk:

| What | Where | Keeps |
|---|---|---|
| `orchestration.trace` | `POST /orchestrate` response | clipped at `ORCHESTRATOR_TRACE_CHARS` (4000) per string, with a "…N more chars" marker |
| `GET /runs` | list of recent runs (intent, agents, calls, ms, answer preview) | summaries |
| `GET /runs/{run_id}` | one full record | unclipped |
| `logs/orchestrator/runs.jsonl` | one JSON line per run | unclipped |
| `logs/orchestrator/<run-id>.json` | the same record, pretty-printed | unclipped |

```bash
python -m scripts.logs runs                  # table: run id, intent, agents, calls, ms, answer
python -m scripts.logs runs --run <id>       # every step, with prompt/tool previews
python -m scripts.logs runs --run <id> --full  # full JSON details
curl -s localhost:8000/runs | python -m json.tool
```

The sidebar's **Runs** panel lists the same runs — `Saved` for
`logs/orchestrator/`, `This tab` for the ones this browser remembers; clicking
one loads the stored record into the inspector, so a run can be audited after the
page has been reloaded. The inspector scrolls inside its column: a 20 000-line
record never stretches the page.

### Watching a run: live thoughts

A run with four agents and a review takes seconds to a minute, and for all of
that the interesting part is invisible. So the page does not wait for one JSON
body: it reads `POST /orchestrate/stream` as server-sent events and draws the
run **while it happens**, above the answer.

```
open     {run_id}                     the run exists, work starts
thought  {...}                        one trace event, as it is written
result   {...}                        the finished payload — identical to POST /orchestrate
error    {...}                        the failure body, when the run could not finish
```

A thought is one line of the trace, projected for a reader rather than an
auditor (`app/orchestrator/live.py`):

```json
{"seq": 7, "category": "agent", "group": "agent:search", "label": "Web researcher",
 "kind": "reply", "title": "Model reply (step 1)",
 "text": "The 3.14 release notes…", "offset_ms": 4210, "at": "2026-11-04T09:12:44Z",
 "detail": {"wave": 1, "model": "rayen-gemma4-31b"}}
```

| | |
|---|---|
| `group` | what the page folds by — `planner`, `agent:<name>`, `evaluator`, `manager` |
| `kind` | `note` · `prompt` · `reply` · `tool` · `verdict` · `rewrite` · `status` |

Three rules keep it honest and cheap:

- **no hidden state, no system prompts** — a `prompt` thought carries the *last
  user message*, i.e. the task the agent was actually given, never the
  instructions around it;
- **no double reveal** — the synthesis reply *is* the answer, so it travels as a
  `status` ("wrote the answer, 812 chars") and the answer arrives once, in the
  bubble;
- **bounded** — strings are clipped in the projection (700 chars, errors 240),
  because this goes over a socket that stays open, not into a file. The
  unclipped record is still in `logs/orchestrator/`.

```bash
curl -N -s -X POST localhost:8000/orchestrate/stream \
  -H 'content-type: application/json' \
  -d '{"message":"what changed in the latest Python release?"}'
```

The run itself is untouched by the stream: same manager, same trace, same
memory write. A browser that disconnects mid-run costs the socket, nothing more
— the run finishes and is stored as usual. The relay is one-way and defensive:
because a thought is decoration and the run is the product, a projection that
ever fails turns that one line into `plain_thought()` (who + what, no detail) and
logs a warning, so **nothing about the stream can turn a good run into a 500**.
Validation errors (unknown file, consent pending, orchestrator disabled) are
still plain JSON with their status code, so nothing is streamed for a request
that never started. `POST /orchestrate` keeps working exactly as before for
scripts.

### Evaluating the orchestration itself (the Evaluate button)

Everything above answers *what did this run do*. The audit answers a different
question: **is the machine that produced it still sound?** It runs the real
pipeline through its ports with scripted models, search and agent loops, so it
needs **no ARAS service and no network**, finishes in about a third of a second,
and never writes a run record or touches the conversation memory.

Press **Tools ▾ → Evaluate** (or `POST /audit/run`). The panel lists all
eighteen checks *before* anything runs, then fills them in one at a time as the
verdicts land — a check per part of the machine, not one global score:

| Level | What it proves |
|---|---|
| `plan` | selection coverage on a golden set, model-written briefs (and the fallback to rule-written briefs when the model's JSON is broken), wave scheduling incl. dependency chains and cycles |
| `execute` | a failing agent is isolated, a slow one is fenced by the timeout, a dependent step really runs in a later wave |
| `gate` | the no-model evaluator's verdicts, and that a rewrite cannot drop a URL the report had |
| `manager` | one agent means no review; a real review runs once and is capped; with no model the manager merges instead of inventing |
| `record` | the run record serialises with every promised field; the live stream delivers each event once, in order, projectable |
| `outcome` | the answer cites only sources the run found; the same request plans identically five times |
| `robust` | a 200 000-character file, an empty prompt, an invented agent name; instructions hidden in an attachment cannot re-plan the run |
| `live` | *(opt-in)* the model planner vs the rules, and the per-stage cost of a real run — `skip` with the reason when the router is not configured |

```
GET  /audit          the catalogue + the last scorecard (what the panel shows on open)
POST /audit/run      run every check → the scorecard ({"live": true} adds the live ones)
GET  /audit/stream   the same run, streamed: open · start · check · result · error
GET  /audit/last     the newest scorecard on disk
```

Each check reports `status` (`pass` · `warn` · `fail` · `skip`), the `detail`
sentence you read in the row, `ms`, `metrics` (the numbers the verdict came
from, shown in the row's *metrics* disclosure) and `findings` (every line the
verdict rests on). The whole scorecard is one JSON object, shown in the panel's
JSON tab, copyable, downloadable, and written to `logs/audit/`:

```
logs/audit/<audit_id>.json     the machine record (and latest.json, a copy)
logs/audit/<audit_id>.md       the same as a markdown table, for a changelog or a diff
```

From a terminal, for CI:

```bash
python -m scripts.audit                 # table, exit 1 on any failure
python -m scripts.audit --json          # scorecard on stdout
python -m scripts.audit --live          # also the two checks that call the router
python -m scripts.audit --only gate.rules,plan.waves
```

Two rules make the result trustworthy. **The sixteen default checks never call
the network** (only the two opt-in `live` ones do), so a red line there always
means the code changed rather than a service having a bad minute. And **a check
that raises is reported as a failure, not an exception** — the audit survives
its own bugs and shows you the traceback as a finding.

Because the checks exercise the ports rather than ARAS, they are the
orchestration layer's own tests: they describe the orchestrator whatever is
wired behind it.

### Attachments

`POST /files {"name", "data_b64"}` (the UI base64-encodes what the browser
already has, so no multipart dependency) or `{"name", "text"}`. The reply
carries the file's `id`; a request reads a file when that id is in its
`file_ids` — `POST /orchestrate {"message": "…", "file_ids": ["<id>"]}` — which
the page sends for every attached file. An id that is not in the store answers
`404 file_not_found`.

| Type | How |
|---|---|
| `.txt .md .csv .tsv .json .yaml .xml .html` + code | decoded as text; CSV/TSV/JSON also parsed into rows + header |
| `.docx` | Word is a zip → `word/document.xml` |
| `.xlsx` | `xl/worksheets` + `xl/sharedStrings` |
| `.pdf` | the ARAS OCR service (:8102) when it is configured (`ARAS_OCR_TOKEN`); when it is not, or it fails or returns no text, a local parse with `pip install pypdf`; with neither, **415 with that instruction** |
| `.doc` | rejected: save as `.docx` or `.txt` |

Stored under `FILES_DIR` (`.data/files`, git-ignored): text + JSON sidecar, so an
upload survives a restart. `GET /files` lists, `DELETE /files/{id}` removes.
Limits: `FILES_MAX_MB=10`, `FILES_MAX_CHARS=200000` (truncation is reported),
`FILES_CONTEXT_CHARS=12000` of excerpts per document prompt. With
`ARAS_AUTO_INGEST=true` an accepted upload is also indexed into
`ARAS_COLLECTION` through the ingestion service, and the upload reply says how
it went under `aras_ingest`.

**Permission.** A file is data the agent reads on your behalf, so there is a
switch for asking first:

```env
# show an allow / don't-allow prompt before an agent reads a file (off = allow)
FILES_PERMISSION_PROMPT=false
```

- **Off (default)** — this is a trusted workspace, so a file is allowed the
  moment it is attached. No prompt, no extra click.
- **On** — attaching a file shows an **Allow / Don't allow** card in the composer,
  and *Send* waits for your answer. "Don't allow" drops the file; nothing is
  uploaded or read.

The API enforces it too, so a script cannot skip the prompt: an upload without an
answer is stored **pending** (`consent: false`, `needs_consent: true`),
`POST /orchestrate` answers `403 consent_required` while it is pending, and
`POST /files/{id}/consent {"granted": true|false}` allows it or deletes it.

If the server cannot write `FILES_DIR` (a permission error on Windows is the
usual case), the upload still works — the file is kept in memory for the session
— and the response carries `stored: false` plus a `warning` naming the error;
`/health.files.writable` says the same.

### Harness memory: the conversation, not the log

The orchestrator, the agents and the tools make up the harness; memory is what
lets it pick a conversation back up. It is a **separate base** from the logs:

| Base | Question it answers | Lifetime | Holds |
|---|---|---|---|
| `logs/` | what did the process do? | rotated | log lines, tracebacks, requests |
| `logs/orchestrator/` | what did the machine do in one run? | one JSON per run | prompts, tool calls, timings, unclipped |
| `memory/` | **what did we say to each other?** | until you forget it | every turn, both sides, exactly as rendered |
| `.data/files/` | what was uploaded? | kept | attachment text + metadata |

```
memory/
  index.jsonl                 append-only index (newest state per session)
  sessions/<session-id>.json  the conversation: every turn, both sides
  sessions/<session-id>.md    the same conversation as readable markdown
```

A turn keeps the request **and** the JSON payload the page rendered, so
reopening a session is a re-render, not a regeneration — same bubble, same
*How this answer was built* panel, same trace, **zero** model calls.

```bash
# continue a conversation from anywhere (the server owns the context)
curl -s -X POST localhost:8000/orchestrate -H 'content-type: application/json' \
  -d '{"message":"and the refunds?","session_id":"s-20261101-091403-a1b2c3"}' | python -m json.tool

python -m scripts.memory list                     # conversations in memory/
python -m scripts.memory show <session-id>        # you │ ai, turn by turn
python -m scripts.memory search refund            # grep every conversation
python -m scripts.memory export <id> -o chat.md   # markdown transcript
python -m scripts.memory forget --all             # clear memory
```

| Setting | Default | Meaning |
|---|---|---|
| `HARNESS_MEMORY` | `true` | store conversations at all |
| `MEMORY_DIR` | `memory` | where they live (git-ignored) |
| `MEMORY_HISTORY_TURNS` | `6` | earlier turns handed back to the model |
| `MEMORY_HISTORY_CHARS` | `3000` | clip per remembered message |
| `MEMORY_MAX_TURNS` | `200` | oldest turns dropped past this |

`POST /orchestrate`, `/chat` and `/search` take `session_id` (created when
unknown, returned in `session`) and `remember` (`false` = do not store this
exchange). When the session has turns, the model context is rebuilt from
memory and the `history` array a browser sends is **ignored** — one source of
truth, and a reloaded tab still continues properly.

In the UI the **Memory** panel lists the conversations; clicking one re-renders
it with its traces and you keep chatting. The pill in the composer shows which
session you are in, `+ New` starts a fresh one, and the page reopens your last
session on reload.

JSON rather than SQLite on purpose: a conversation is a small self-contained
document — greppable, diffable, backup-able with `cp`, readable without the app.
The layout mirrors the run log (index for listing, one file per item for the
detail), so both bases work the same way. An unwritable `MEMORY_DIR` degrades to
in-process memory and reports `writable: false` in `/health`.

---

## 6. The page

`app/static/` is one HTML file, three stylesheets and one script — no build
step, no dependency, no CDN (the Markdown renderer is local). Design reference:
[`docs/redesign/`](docs/redesign/README.md). Three columns, each scrolling on
its own inside the screen:

| Column | What is in it |
|---|---|
| top bar | brand, the light/dark switch, and two dropdowns — **Status ▾** (the `MODEL` / `SEARCH` / `ARAS` / `Diag` / `Audit` pills as rows, with one aggregate dot on the button) and **Tools ▾** (**ARAS services**, **ARAS wiring**, **Run diagnostics**, **Evaluate**) |
| sidebar | workspace switcher · nav (Overview · Run history · Memory · ARAS services) · current session + the conversations in `memory/` (click to reopen) · **Runs** (`Saved` = `logs/orchestrator/`, `This tab` = your own runs) · **Agents** (live roster) · **ARAS services** (a filled dot when a service is ready, the variables to set when it is not) · health card |
| chat | the conversation as a reading surface — Markdown answers (tables, code, callouts), source chips, the live thoughts, and the run panel with one chip per agent and every step of the run underneath |
| inspector | three tabs — **Trace** (run summary, numbered timeline with durations, tool activity, readable diagnostics), **Context**, **JSON** |

The composer carries the mode control, **Attach**, **New chat** and
**Run prompt**; `Enter` runs, `Shift+Enter` adds a line.

| Mode | Route | What runs |
|---|---|---|
| **Auto** | `POST /orchestrate/stream` | the manager, the agents, the evaluator (§5) |
| **Agent** | `POST /chat` | one model + the `web_search` tool (§4) |
| **Answer** | `POST /chat`, `allow_search: false` | the model alone |
| **Search** | `POST /search` | the ARAS web service alone |

The page negotiates where the API lives (the app root, a sub-path, or `/api`
behind a proxy) and shows a red banner listing what it tried when nothing
answers. Both column edges drag to resize; the widths and the theme are
remembered per browser.

---

## 7. HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the studio page |
| `GET` | `/health` | is the model ready, is the search ready, what is missing (no secrets, no network call) |
| `GET` | `/config` | effective settings summary (no secrets) |
| `GET` | `/providers` | the ARAS services: role, endpoint, state, the variables still missing (§2) |
| `GET` | `/aras` | live health of every configured ARAS service + readiness per capability |
| `GET` | `/aras/models` | the model ids the router lists — where a value for `ARAS_MODEL` comes from |
| `GET` | `/diagnose` | a real completion + a real search + every health probe + hints (§3) |
| `POST` | `/search` | web search through the ARAS web service, no model |
| `POST` | `/chat` | the single-agent loop: model + `web_search` tool, returns the trace (§4) |
| `POST` | `/orchestrate` | **Auto mode**: manager plans, agents run in parallel, manager synthesises (§5) |
| `POST` | `/orchestrate/stream` | the same turn, streamed as it happens (SSE: `open`, `thought`, `result`, `error`) — what the page uses |
| `POST` | `/files` | upload an attachment (`{name, data_b64}` or `{name, text}`, optional `consent`), returns its summary |
| `GET` | `/files` | attachments currently in the store (+ how many wait for permission) |
| `POST` | `/files/{id}/consent` | the user's answer: `{"granted": true}` allows, `false` deletes |
| `DELETE` | `/files/{id}` | remove one attachment |
| `GET` | `/agents` | agent roster, planner mode, evaluator/review/synthesis flags, readiness |
| `GET` | `/audit` | the self-audit catalogue + the last scorecard |
| `POST` | `/audit/run` | run every check offline (scripted ports) → scorecard; `{"live": true}` adds the model checks, `{"only": [...]}` narrows it |
| `GET` | `/audit/stream` | the same run as SSE (`open`, `start`, `check`, `result`, `error`) — what the Evaluate panel reads |
| `GET` | `/audit/last` | the newest scorecard from `logs/audit/` |
| `GET` | `/runs` | recent orchestration runs from `logs/orchestrator/` (intent, agents, model calls, timing) |
| `GET` | `/runs/{run_id}` | one run, full unclipped trace (prompts, tool calls, results) |
| `GET` | `/sessions` | conversations in memory (title, turns, last message, `?q=` filter) |
| `POST` | `/sessions` | start an empty conversation (`{title, mode}`) |
| `GET` | `/sessions/{id}` | every turn of a conversation, with the payload to replay it |
| `PATCH` | `/sessions/{id}` | rename a conversation |
| `DELETE` | `/sessions/{id}` | forget a conversation |
| `GET` | `/version` | app version (also in `/health` and in the page `<meta>`) |

**Every route above is also served under `/api`** (`/api/chat`, `/api/health`, …), and
`GET /api` lists them. That prefix exists because a proxy often adds one; with it the
page keeps working instead of turning into `{"detail":"Not Found"}`. Unknown paths
return JSON with a `hint` and the route list, and `HEAD /` is answered.

Errors are structured, never silent:

| Status | `error` | When | The body carries |
|---|---|---|---|
| `400` | `configuration` | a service the request needs is not configured | `detail.service`, `detail.missing` (the env vars), `detail.hint` |
| `502` | `aras` | an ARAS service refused, failed or did not answer | `detail.service`, `detail.status`, the service's own message, `detail.hint` |
| `403` | `consent_required` | an attached file still waits for permission | `files` (the ones waiting), `hint` |
| `404` | `not_found` / `file_not_found` | an unknown path, run, session or attachment | `hint` |
| `413` / `415` | `file` | an oversized or unreadable attachment | `hint` |
| `503` | `disabled` | `ORCHESTRATOR_ENABLED=false` | `hint` |
| `422` | — | request validation | the field errors |

```json
{
  "error": "configuration",
  "message": "[aras:router] not configured — missing ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL",
  "detail": {
    "provider": "aras", "service": "router", "status": null,
    "message": "[aras:router] not configured — missing ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL",
    "hint": "set ARAS_BASE_URL (or ARAS_ROUTER_URL), ARAS_ROUTER_TOKEN (or the shared ARAS_TOKEN), ARAS_MODEL (a router model id — GET /aras/models lists them) in .env, then restart",
    "missing": ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"]
  },
  "session": {"session_id": "s-20261110-052230-80fab4", "…": "…"}
}
```

Every message starts with `[aras:<service>]`, so the service that failed is
readable without parsing anything.

---

## 8. Layout

```
app/
  orchestrator/        PART 1 — the orchestrator: portable, talks to the app only through host.py
    service.py         entry point: OrchestratorService(settings, models=, search=, agent_loop=, …)
    contracts.py       ports: model, search, agent loop, knowledge store, data backend, settings
    host.py            the only file that imports the app (logger, log dir, loop, settings)
    README.md          the service contract + the porting checklist
    manager.py         classify -> plan -> waves -> evaluate -> review -> synthesis, totals, errors
    planner.py         rules + model planner, per-agent briefs, dependency graph
    executor.py        waves: asyncio.gather + semaphore + timeout, failures isolated
    evaluator.py       score + rewrite each report before the manager reads it (rules when no model)
    agents.py          document / search / data / general workers
    trace.py           every step of a run (prompts, tools, replies), clipping
    live.py            the trace as "thoughts" for the stream
    runbus.py          delivers a run's events to whoever watches it
    runlog.py          logs/orchestrator/ records: save, list, read
    audit.py           the self-audit: eighteen checks through scripted ports
    files.py           upload decoding, extraction (txt/csv/json/docx/xlsx/pdf), store
  static/              PART 2 — the UI
    index.html         the studio page: top bar, sidebar, chat, inspector, modals
    assets/css/        styles.css (design system) + extra.css (components) + theme.css (light)
    assets/js/app.js   the whole page logic, no dependencies
  aras.py              PART 3 — ARAS: one client per service + the adapters that fill the ports
  main.py              the HTTP routes, served at / and /api — where the three parts are wired
  agent.py             the single-agent loop (model + web_search): /chat and the search agent
  config.py            every setting, read from the environment / .env
  memory.py            harness memory: conversations in memory/ (store, replay)
  logs.py              logging: rotating files, redaction, request lines, crash capture
  version.py           the one version number
run.py                 server launcher: preflight + banner + logs + startup record
scripts/
  aras_smoke.py        standalone ARAS probe: tunnel, health, tokens (stdlib only, copy it anywhere)
  audit.py             the self-audit from a terminal / CI
  logs.py              read the logs: list | tail -f | errors | requests | process | runs
  memory.py            conversations: list | show | search | export | forget
tests/                 offline tests (MockTransport: no ARAS, no network) + three jsdom page tests
docs/redesign/         the design reference the UI was built on (preview.png + prototype)
logs/                  runtime logs (git-ignored) + logs/changelog/ release notes
logs/orchestrator/     one JSON record per Auto run (trace, prompts, timings)
memory/                the conversations themselves: sessions/*.json + .md (git-ignored)
.data/files/           uploaded attachments (runtime, git-ignored)
```

`tests/test_orchestrator_service.py` pins this list: `app/` holds exactly the
three parts and the six plumbing modules above, so a new backend cannot arrive
unnoticed.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `400 configuration … [aras:router] not configured — missing …` | The router has no URL, no token or no model id. The message lists the variables; fill them in `.env` and restart |
| `MODEL  not configured` pill, but `.env` is filled | The server was started before the edit, or from another folder. `.env` is read from the working directory at start; `GET /health` → `env_file` says whether one was found |
| `502 aras … 401 unauthorized — the token was rejected` | That service's token is wrong or was set inactive. Check `ARAS_<SERVICE>_TOKEN` (or the shared `ARAS_TOKEN`); `python scripts/aras_smoke.py` tests each token on its own |
| `502 aras … HTTP 403` | The token was read but may not do this — usually another service's token in the wrong line |
| `502 aras … the router refused ARAS_MODEL=…` | The id is not one this token may call. Copy one from `GET /aras/models`; the ids with the `sharif-` prefix are the ones the router token opens |
| `ARAS_MODEL=auto` is reported as missing | Nothing picks a model for you. Write the id |
| `502 aras … the router refused the tool-calling request` | The model cannot do function calling, which the search agent needs. Pick another id for `ARAS_MODEL`, or use **Answer** mode |
| `502 aras … network error talking to http://127.0.0.1:81…` | The SSH tunnel is closed or does not forward that port (§2). `python scripts/aras_smoke.py` shows which ports are open |
| every service answers `HTTP 503` at once | Something is replying for `127.0.0.1` — usually a VPN or system proxy. Unset `HTTP_PROXY`/`HTTPS_PROXY` or add `127.0.0.1` to `NO_PROXY`, then compare with `curl http://127.0.0.1:8101/api/v1/health/` |
| `502 aras … no answer from … within 60s` | The connection opened, so the tunnel is up; the service is slow. Raise `ARAS_TIMEOUT` (model calls can take a while) |
| `502 aras … HTTP 301: redirected to …` | The URL is not the route itself. Check `ARAS_BASE_URL` / `ARAS_<SERVICE>_URL`: the router's must end with `/api/v1`, and nothing may strip the trailing slash on the way |
| `502 aras … HTTP 429` | The router is rate limiting this token. Wait, then retry |
| the search agent reports `failed` | Web search is not configured (`ARAS_WEB_TOKEN`) or no search came back from the web service; the report carries the service's message and hint, and the rest of the run continues |
| the document agent is `skipped`: *no attachments … no knowledge store is wired in* | Nothing was attached and retrieval is not ready: it needs `ARAS_RETRIEVAL_TOKEN` **and** `ARAS_COLLECTION`. `GET /providers` shows which is missing |
| a question about the indexed collection is answered without retrieval | The rules plan the document agent only for attached files, so the generalist took it. Send `"planner": "llm"` (or set `ORCHESTRATOR_PLANNER=llm`): the model can then pick the document agent, which searches `ARAS_COLLECTION` |
| the data agent never asks the data service | It needs `ARAS_DATA_TOKEN`, `ARAS_TENANT`, `ARAS_OWNER` and `ARAS_DATA_SOURCE_ID` — and it computes attached tables and arithmetic itself first |
| a PDF upload answers `415 … needs the 'pypdf' package` | No reader produced text and there is no local parser. Either the OCR service is not configured (set `ARAS_OCR_TOKEN`), or it is and the call failed — the upload does not say which: `python scripts/aras_smoke.py` shows a closed port or a rejected OCR token. `pip install pypdf` adds the local parser |
| chat is empty after a reload / `"session": null` | Memory is off or `MEMORY_DIR` is not writable. Check `GET /health` → `memory`, set `HARNESS_MEMORY=true`, and `python -m scripts.memory list` |
| `{"detail":"Not Found"}` on send | The page is older than the server. Reload: the composer shows the running version (`· v…`), and both `/chat` and `/api/chat` answer |
| Buttons dead, modes stacked inline | CSS/JS did not load. Hard-reload (`Ctrl+Shift+R`); a red banner says so when it happens |
| an upload answers `stored: false` with a `warning` | `FILES_DIR` is not writable (a permission error on Windows is the usual case). The file is kept in memory for this session and `GET /health` → `files.writable` is `false`; point `FILES_DIR` at a writable folder to persist it |
| "Permission needed" card in the composer | `FILES_PERMISSION_PROMPT=true`: allow or refuse each attached file. Set it back to `false` to allow every file at once |
| variables from an older `.env` | Settings this app no longer has are ignored, not an error. `logs/changelog/v0.19.0.md` lists what was removed |
| Anything else | `python -m scripts.logs errors` (tracebacks), `... requests --status 5xx`, `... tail -f` |

## 10. Logs

Everything goes to `logs/` (override with `LOG_DIR`). Files are created on start, rotate at
5 MB × 5, and are git-ignored — only `logs/README.md` and `logs/changelog/` are committed.

Logs are diagnostics, not content: the conversation itself lives in `memory/`
(§5, "Harness memory"). Use `scripts.logs` for "what did the process do?" and
`scripts.memory` for "what did we say?"

| File | What it holds |
|---|---|
| `agetnic.log` | everything at `LOG_LEVEL`+, every line carries the `pid` and logger name |
| `error.log` | warnings, errors and **full tracebacks** — start here when something breaks |
| `requests.log` | one line per HTTP call: method, path, status, ms, request id, client, bytes |
| `terminal.log` | mirror of stdout/stderr — what the terminal window printed, uvicorn included |
| `process.log` | one JSON line per start: pid, ppid, cwd, argv, python, platform, git commit, port, preflight problems |
| `crash.log` | `faulthandler` dump for a segfault or hard abort |
| `archive/` | rotated files (`agetnic.log.1`, `terminal-20261031-141702.log`, …) |

```bash
python run.py                        # writes the files above
python -m scripts.logs list          # files, sizes, last write
python -m scripts.logs tail -f       # follow the app log
python -m scripts.logs tail error -n 100
python -m scripts.logs errors        # warning and error records, with tracebacks
python -m scripts.logs requests --status 5xx --slowest 5
python -m scripts.logs process       # every start: pid, port, cwd, git
```

A line looks like:

```
2026-11-10 09:27:19 WARNING  pid=1803 agetnic.chat: chat failed: [aras:router] not configured — missing ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL
2026-11-10 09:27:19 WARNING  pid=1803 agetnic.requests: POST /api/chat -> 400 7.9ms req=970f99412b client=127.0.0.1:52144 bytes=779
```

The `req=` id is returned in the `X-Request-ID` response header and in every JSON 500, so a
number from the browser leads straight to its lines. An unhandled exception answers
`500 {"error":"internal", "request_id": …}` and writes the traceback to `error.log`.

**Secrets are redacted** before writing (tracebacks included): anything shaped like
`Bearer …`, `token=…`, `api_key=…`, `secret=…` or `password=…`, plus the value of every
variable in the process environment named `*_TOKEN`, `*_API_KEY`, `*_SECRET` or
`*_PASSWORD`, becomes `«redacted»`. A token written only in `.env` is not in the process
environment, so it is caught by the shape rules rather than by its value.

Prompts and answers are not logged at any level — they are in the run records
(`logs/orchestrator/`) and in `memory/`. An error line may quote the first few hundred
characters of what a service replied.

Tuning: `LOG_LEVEL` (default `INFO`), `LOG_ROTATE_MB` (5), `LOG_BACKUPS` (5), `LOG_DIR`.
These four are read from the **process environment**, not from `.env` (the logger does
not parse that file), so export them — or override one run with
`python run.py --log-level DEBUG --log-dir /tmp/agetnic`.

`logs/changelog/` holds release notes, one markdown file per version, indexed in
[`logs/changelog/README.md`](logs/changelog/README.md) — those are notes, not logs.

## 11. Design notes

- **Three parts, one seam each.** The orchestrator asks for ports and never
  imports a backend; ARAS lives in one module that fills those ports; the page
  only reads what the API returns. Each part can be read, tested and replaced
  without opening the other two.
- **One backend, on purpose.** A second model or a second search engine is a
  second way for an answer to be produced and a second thing to debug. Here a
  request has exactly one path, so "which model answered?" has one answer and a
  failure has one place to look.
- **No mock, no fallback.** A silent substitute makes a broken token look like
  a working system. Missing configuration is an error with instructions; a
  service that fails is named in the reply.
- **Errors are data.** Tool and service failures are returned to the model
  *and* to the caller, so the agent can report a failed search truthfully, and
  a failed agent is a row in the run record rather than a lost request.
- **Docs are tested.** `tests/test_docs.py` asserts that `.env.example` holds
  every ARAS setting (the per-service URL overrides aside — they are in §2) and
  nothing the app does not read, that this file covers every service and every
  route, and that no secret-looking value is committed.
- **The page is a view of the record, not a story about it.** The chat surface, the plan
  chips and the inspector render the same payload the log file holds: if a step happened, it
  is on screen; if it did not, nothing is drawn. Markdown is parsed locally into DOM nodes
  (no CDN, no `innerHTML`), so an offline sandbox still reads correctly and model output
  cannot inject markup.
- **Logs are part of the product.** A request that fails must leave a line you can find:
  status, duration, request id, client, and a traceback in `error.log` when it was a crash.
  Redaction runs on the way out, so debugging never means pasting a token into a chat.
