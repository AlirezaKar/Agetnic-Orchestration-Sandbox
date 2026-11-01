# Agetnic Orchestration Sandbox

A small backend skeleton where **the model is pluggable** (free providers only) and the
**web-search layer is pluggable too**, with an agent loop that actually calls the tools.

- No mock model. If no provider is configured, every call fails loudly with the exact
  env var that is missing and a link to get it. Nothing is faked.
- One provider code path (OpenAI-compatible chat completions) covers Groq, Gemini,
  OpenRouter, Mistral, Cloudflare, NVIDIA, Cerebras, Hugging Face, Ollama, LM Studio and
  **any custom endpoint**, so switching the brain is one line in `.env`.
- Web search: keyless backends that need **no background server** (DuckDuckGo, Wikipedia),
  free-key backends that need no server either (Tavily, Serper, Exa, Google CSE), and
  SearXNG only as an opt-in self-hosted option.
- **Auto mode**: a manager plans the request, runs the needed agents (document ·
  web search · data · general) **in parallel**, and writes one answer from their
  reports. Attach a file and the document agent reads it.
- Built-in way to **verify** all of it: `python -m scripts.doctor`, `GET /diagnose`,
  `GET /search/diagnose`, plus an offline test suite.

> Note: this repository was an empty stub (`README.md` only) when this work started, so the
> whole backend was scaffolded here rather than patched. Everything below is new code.

---

## 1. Quickstart (2 minutes)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# open .env: keep LLM_PROVIDER=auto, fill ONE provider key + its model (see §2)

python -m scripts.start           # checks, then the server on http://localhost:8000
```

`scripts.start` is the whole quickstart in one command, in this order:

| Step | Command it runs | What it tells you |
|---|---|---|
| 1 | `python -m scripts.check_providers` | which providers are ready, which env var is missing, where to get it |
| 2 | `python -m scripts.doctor` | does the model actually answer, does search actually answer |
| 3 | `python run.py` | uvicorn serving the UI **and** the API on one port |

`--agent` and `--orchestrate` add optional round trips to the doctor step:
the single-agent loop, and the manager (plan → parallel agents → wrap-up).

If a check fails it stops before starting the server (add `--force` to start anyway,
`--check-only` to stop after the checks, `--no-checks` to skip them).
The individual commands still work on their own:

```bash
python -m scripts.check_providers                            # table + what is missing
python -m scripts.check_providers --provider groq --models    # live ids, best first
python -m scripts.doctor --agent                              # include a full agent round trip
python -m scripts.doctor --orchestrate                        # include a manager run (plan + agents)
python -m scripts.check_search -q "python 3.13 release notes"
python3 run.py --reload                                       # server only
```

`run.py` is the supported entry point: it starts **one** process that serves both the page
and the API, prints the URLs/routes, warns about a missing `.env` or missing assets, and
starts the logs described in §9 (`logs/agetnic.log`, `error.log`, `requests.log`,
`terminal.log`, `process.log`). Plain `uvicorn app.main:app --port 8000` works too — it
configures the same files on startup.

Open <http://localhost:8000> — the **test harness** (the original PR #1 UI, rebuilt on this
API): mode selector (**Auto** / Agent / Answer only / Search only), 📎 attach (or drop a
file) for the document agent, a manager-plan panel with one card per agent, search-backend
list, diagnostics panel, run history, trace inspector, and a providers modal with every
key/model link. Auto mode is described in §5.

```bash
python -m unittest discover -s tests -v     # offline, no keys, no network
```

---

## 2. Choosing the core model (all free)

Two independent choices: **which provider** and **which model**.

- `LLM_PROVIDER=auto` (default) — the first provider in the order below that has **both** a
  key and a model is used. No network call, no guessing.
- `LLM_PROVIDER=<name>` — that provider is used, even if others are also configured.
- The **model is always written by you**, on the line under its API key
  (`GROQ_MODEL=…` next to `GROQ_API_KEY=…`). Nothing is substituted or invented; the id you
  write is the id that is called. If it is missing, the provider counts as not ready and the
  error tells you which line to fill and where the live ids are.

```dotenv
LLM_PROVIDER=auto          # or: groq, gemini, openrouter, mistral, cerebras, nvidia,
                           #     cloudflare, huggingface, ollama, lmstudio, custom
GROQ_API_KEY=gsk_…
GROQ_MODEL=openai/gpt-oss-120b
```

Auto order: `groq → gemini → openrouter → mistral → cerebras → nvidia → cloudflare →
huggingface → ollama → lmstudio → custom`. A key without a model does not win; the next
provider in the order is tried. `LLM_FALLBACKS=gemini,openrouter` puts those first instead.

| Provider | Free tier | Get an API key | Model list |
|---|---|---|---|
| `groq` | ~30 req/min, 14.4k req/day on 8B, no card | <https://console.groq.com/keys> | <https://console.groq.com/docs/models> |
| `gemini` | Free tier per model, 1M context, no card | <https://aistudio.google.com/apikey> | <https://ai.google.dev/gemini-api/docs/models> |
| `openrouter` | ~20 `:free` models, 20 req/min, 50 req/day | <https://openrouter.ai/keys> | <https://openrouter.ai/models?max_price=0> |
| `mistral` | Free "Experiment" plan, ~2 req/min | <https://console.mistral.ai/api-keys> | <https://docs.mistral.ai/getting-started/models/models_overview/> |
| `cloudflare` | 10,000 neurons/day, no card | <https://dash.cloudflare.com/profile/api-tokens> | <https://developers.cloudflare.com/workers-ai/models/> |
| `nvidia` | Free developer credits, no card | <https://build.nvidia.com/> | <https://build.nvidia.com/models> |
| `huggingface` | Small free monthly credits | <https://huggingface.co/settings/tokens> | <https://huggingface.co/models?inference=warm> |
| `cerebras` | Free trial credits then card | <https://cloud.cerebras.ai/> | <https://inference-docs.cerebras.ai/models/overview> |
| `ollama` | Local, unlimited, no account | <https://ollama.com/download> | <https://ollama.com/library> |
| `lmstudio` | Local, unlimited, no account | <https://lmstudio.ai/> | <https://lmstudio.ai/models> |
| `custom` | whatever your endpoint offers | *(your provider)* | *(your provider, or `GET /v1/models`)* |

### Model ids retire — copy a live one

A retired id fails 100% of the time. Both of these were real:

| Symptom | Provider's own message | Meaning |
|---|---|---|
| `[gemini] … HTTP 404` | *"models/gemini-2.5-flash is no longer available to new users, use models/gemini-3.8-flash"* | the key and endpoint are fine, the **model id is retired** |
| `[openrouter] … HTTP 400/404` | *"No endpoints found for meta-llama/llama-3.3-70b-instruct:free"* | the id exists in the catalogue but has **no live endpoints** |

So ask the provider what your key can call, instead of trusting a blog post:

```bash
python -m scripts.check_providers --provider gemini --models   # live ids, best first
python -m scripts.check_providers --provider gemini --models --autofix   # writes the best id into .env
```

`--autofix` writes the recommended id into `.env` (backup: `.env.bak`) — it still ends up as
an explicit `<PROVIDER>_MODEL` line, which is the only thing the runtime reads.

Notes that matter:

- **Tool calling is required** for web search. Llama/Qwen/Gemini/Mistral models support it;
  tiny 1–3B models often do not, and the 400 error says so.
- `gemini` goes through Google's OpenAI-compatibility endpoint (`/v1beta/openai`), which is
  why one provider code path covers all of them.
- `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` are advanced overrides for a **manually**
  chosen provider (point a preset at a proxy). In `auto` mode they are ignored, and
  `check_providers` says so.
- Want the old "let the backend pick a live model" behaviour? Put `auto` in that provider's
  model line (`GROQ_MODEL=auto`) or set `LLM_AUTO_SUBSTITUTE=true` to fall back to a live id
  when yours has been retired. The response reports which model answered.

### Failover

```dotenv
LLM_PROVIDER=groq
LLM_FALLBACKS=gemini,openrouter
```

Tried left to right; the first provider that answers wins. A 429 on Groq silently moves to
Gemini. Each fallback uses its own `<PROVIDER>_API_KEY` / `<PROVIDER>_MODEL`, so a fallback
that has no model line is skipped with a message saying which line to fill.

### Custom provider

Any OpenAI-compatible `/v1` endpoint (vLLM, LiteLLM, llama.cpp server, an internal gateway,
a vendor I did not preset):

```dotenv
LLM_PROVIDER=custom
CUSTOM_BASE_URL=https://your-gateway.example.com/v1
CUSTOM_API_KEY=            # optional, leave empty for keyless gateways
CUSTOM_MODEL=your-model-id
CUSTOM_AUTH_SCHEME=bearer  # bearer | raw | none
CUSTOM_AUTH_HEADER=Authorization
CUSTOM_EXTRA_HEADERS={"X-Org":"sandbox"}
CUSTOM_EXTRA_BODY={"reasoning_effort":"low"}
```

Validation is instant: `python -m scripts.check_providers --provider custom --models`
lists what the endpoint actually serves, and `--ping` proves a completion works.

---

## 3. Web search

`SEARCH_PROVIDERS` is an ordered chain; the **first backend that returns results wins** and
a failing one is reported and skipped.

| `SEARCH_PROVIDER` | Key | Background server? | Free allowance | Key / setup | Docs |
|---|---|---|---|---|---|
| `ddg` | none | **no** | unlimited, unofficial | — | <https://duckduckgo.com/html/> |
| `wikipedia` | none | **no** | unlimited | — | <https://www.mediawiki.org/wiki/API:Search> |
| `tavily` | yes | **no** | 1,000 / month | <https://app.tavily.com/home> | <https://docs.tavily.com/documentation/api-reference/endpoint/search> |
| `serper` | yes | **no** | 2,500 one-off | <https://serper.dev/api-key> | <https://serper.dev/playground> |
| `exa` | yes | **no** | 1,000 / month | <https://dashboard.exa.ai/api-keys> | <https://docs.exa.ai/reference/search> |
| `google_cse` | yes (key + cx) | **no** | 100 / day | <https://developers.google.com/custom-search/v1/introduction> | <https://developers.google.com/custom-search/v1/using_rest> |
| `searxng` | none | **yes** (you host it) | unlimited | `docker run -d -p 8080:8080 searxng/searxng` | <https://docs.searxng.org/dev/search_api.html> |

Default with no configuration at all: `SEARCH_PROVIDERS=ddg,wikipedia` — no key, no server.

```dotenv
SEARCH_PROVIDER=ddg
SEARCH_PROVIDERS=ddg,wikipedia          # priority order, failover included
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT=20
SEARCH_DEDUPE=true
```

The `fetch_url` tool also works without a server: it uses the keyless
[Jina reader](https://r.jina.ai) first and falls back to a plain HTTP GET + tag stripping.

### Why not just SearXNG (the previous assumption)?

SearXNG is the only option here that needs a process kept alive and JSON enabled in
`settings.yml`. It is still supported (`SEARCH_PROVIDER=searxng` +
`SEARXNG_BASE_URLS=http://localhost:8080`), but it is no longer required. Note that public
instances usually block the JSON API, so self-hosting is the only dependable way to use it.

---

## 4. How to verify the web search actually works

Four independent layers, cheapest first.

### Layer 1 — parser/plumbing tests, offline (no key, no network)

```bash
python -m unittest discover -s tests -v
```

These tests use `httpx.MockTransport`, i.e. a fake network, so they prove the *code* is
correct: DuckDuckGo HTML (both `html.` and `lite.` layouts, redirect decoding, ad filtering,
bot-challenge detection), MediaWiki JSON, Tavily/Serper/Exa/CSE mapping, dedupe, failover,
"all backends errored" vs "no hits", the SSRF guard on `fetch_url`, and the agent loop
(search → tool message → cited answer).

They deliberately do **not** prove that DuckDuckGo/Google are reachable from your machine or
that your keys are valid. That is layer 2.

### Layer 2 — CLI against the real endpoints

```bash
python -m scripts.check_search                       # every backend in your chain
python -m scripts.check_search --all                 # also the ones with a key set elsewhere
python -m scripts.check_search -q "python 3.13 release notes"   # real query, full results
python -m scripts.check_search --provider wikipedia -q "asyncio"
```

Healthy output:

```
search backends (query: 'python 3.13 release notes')
------------------------------------------------------------------------
  PASS   ddg          5 results in 412 ms
         - Python 3.13 release notes
           https://docs.python.org/3/whatsnew/3.13.html
  PASS   wikipedia    3 results in 188 ms
------------------------------------------------------------------------
```

Failure output is explicit and tells you what to change (e.g. `bot challenge detected` →
add Tavily or reorder to `wikipedia,ddg`). The report is written to
`artifacts/search-diagnose.json`; a real query goes to `artifacts/search-last-query.json`.

### Layer 3 — HTTP, with the server running

```bash
uvicorn app.main:app --port 8000
curl -s "localhost:8000/search/diagnose?q=python+asyncio" | python -m json.tool
curl -s -X POST localhost:8000/search -H 'content-type: application/json' \
     -d '{"query":"python asyncio","max_results":3}' | python -m json.tool
curl -s "localhost:8000/diagnose" | python -m json.tool          # model + search together
```

`/search/diagnose` runs **every** configured backend once, returns `ok: true/false`,
`count`, `latency_ms`, a sample result and a hint per backend, then a `summary` with
`ok`/`failed` lists. That is the "does it work right now" answer.

### Layer 4 — end to end in the browser

Open <http://localhost:8000>:

1. **Search only** mode → press Send. No model is involved; the chain runs and the answer
   panel shows provider, hits and latency.
2. **Agent** mode → press Send. The answer carries a clickable timeline: model step → tool
   step (arguments, latency, ok/failed) → final answer, plus the `sources` list. That is the
   search call happening, not prose you have to trust.
3. **Run diagnostics** (top bar) → the model ping and every configured backend appear in the
   sidebar with pass/fail + latency, the badge turns green, and the raw report opens in the
   trace inspector.
4. **Providers** (top bar) → every free provider with its *Get API key* and *See models*
   links, plus whether it is configured and whether a search backend needs a server.

Depending on the mode, `/chat` returns `search_provider`, `sources` and the full `trace`, so
the UI never has to imply that a search happened — it shows it.

### What I actually checked in the code (audit notes)

- **Bot challenges.** DuckDuckGo answers datacenter/VPS IPs and rapid polling with an
  `anomaly-modal` page. The client detects `202/403/418/429`, `anomaly-modal`,
  "unusual traffic", or a page with a captcha and no results, and reports it as a failure
  with the fallback hint instead of returning `[]`.
- **Terms of service.** `html.duckduckgo.com/html/` is an *unofficial* endpoint (its
  `robots.txt` disallows `/html`). Fine for personal/local testing, not something to build
  a product on — that is why keyed backends are supported and failover exists.
- **Markup fragility.** DDG's HTML is parsed with a real `HTMLParser` (depth-aware, deduped,
  handles nested `<b>` in snippets, both `result__a`/`result-link` layouts, and
  `duckduckgo.com/l/?uddg=` redirects). If the markup changes you get an explicit error, and
  tests pin the expected shapes.
- **Quotas/HTTP errors.** 401/403/429/402/5xx from keyed providers become structured
  `SearchError`s with the key page or quota explanation — never a silent empty list.
- **Empty vs failed.** "All backends raised" and "all backends legitimately found nothing"
  are different outcomes in the response (`attempts` vs `results: []`), which matters for
  the agent's honesty.
- **Dedupe & limits.** `example.com/x` and `www.example.com/x/` collapse to one result;
  `max_results` is clamped (1–10 in the tool, 20 at the API layer).
- **Timeouts.** `SEARCH_TIMEOUT` (default 20 s) per request, applied to every backend, plus
  a longer timeout for page fetches.
- **No SSRF from the agent.** `fetch_url` refuses non-http(s) schemes and
  localhost/private/link-local hosts.
- **Tool failures reach the model.** A failed search is fed back as a structured error so
  the model can say "search failed" instead of hallucinating results; the agent also
  records `errors[]`.

**Honest limitation:** this sandbox has no outbound access to `duckduckgo.com`,
`api.tavily.com` etc. (only package registries and GitHub), so the network calls here were
verified only against mocked transports, not live. Layers 2–4 are the live checks — run
them on your machine; that is exactly what they are for. In this environment
`scripts.check_search` reports:

```
FAIL   ddg          [ddg] network error: ConnectError: ...
       hint: No outbound internet access from this host, or DuckDuckGo is unreachable.
SKIP   tavily       missing TAVILY_API_KEY
```

which is the intended, non-silent behaviour.

---

## 5. Manager orchestration (Auto mode)

The single-agent loop in §6 is one model with two tools. Auto mode adds a lead:

```
1. classify      rules read the prompt, the files and the roster
2. plan          rules select the agents ─► LLM writes each agent's brief + who waits for whom
3. delegate      wave 1: independent agents run in parallel
                 wave 2: an agent that depends on another receives its result as input
4. review        the manager reads the reports and may ask for one more agent task
5. wrap up       the manager writes one answer from all reports (or merges them deterministically)
```

```
                     ┌── Document analyst ── attachments, quotes ──┐
prompt ─► MANAGER ─► PLANNER ─┼── Web researcher ──── URLs, sources ─┼─► MANAGER ─► answer
                     ├── Data analyst ────── exact figures ──────┤   review     (synthesis)
                     └── Generalist ──────── own knowledge ──────┘   + follow-up
                          wave 1 in parallel → wave 2 uses wave 1
```

| Piece | Where | What it decides |
|---|---|---|
| **Service** | `app/orchestrator/service.py` | the entry point and the wiring: `OrchestratorService(settings, models=…, search=…, agent_loop=…)` |
| **Ports** | `app/orchestrator/contracts.py` | what the orchestrator needs from outside (model, search, agent loop, settings) — protocols, not imports |
| **Host seam** | `app/orchestrator/host.py` | the only file in the package that imports the app (logger, log dir, agent loop, settings) |
| Manager | `app/orchestrator/manager.py` | classify → plan → waves → review → one answer (+ sources, errors, timings) |
| Planner | `app/orchestrator/planner.py` | which agents run, the brief each one gets, and the dependency graph |
| Executor | `app/orchestrator/executor.py` | waves: `asyncio.gather` + semaphore + per-agent timeout; failures stay isolated |
| Agents | `app/orchestrator/agents.py` | document / search / data / general, one signature, one result type |
| Trace | `app/orchestrator/trace.py` | every step of the run: prompts, tool calls, tool results, replies |
| Run log | `app/orchestrator/runlog.py` | one unclipped JSON record per run in `logs/orchestrator/` |
| Memory | `app/memory.py` | the conversation itself: every user turn + answer in `memory/`, replayable |
| Attachments | `app/orchestrator/files.py` | upload decoding, extraction, on-disk store |

### The orchestrator is a service (and it is portable)

The orchestrator is organised like a microservice that runs next to the other
ones — the model providers (`app/llm`), the web search (`app/search`), the
single-agent tool loop (`app/agent.py`), the conversation memory (`app/memory.py`).
It **does not import any of them**. It is handed four ports and one host seam:

| Port | Shape | This app passes |
|---|---|---|
| `ModelChain` | `await complete(messages, tools=None, temperature=…, max_tokens=…)` | `build_chain(settings)` |
| `SearchChain` | `await search(query, limit)` | `build_search_chain(settings)` |
| `AgentLoopFactory` | `factory(model_chain, search_chain, settings, observer=hook)` | `app.agent.WebSearchAgent` |
| `OrchestratorSettings` | 15 plain attributes (`contracts.py`) | `app.config.Settings` |

```python
# app/main.py — the whole host side of the orchestration
service = OrchestratorService(settings, models=chain, search=search_chain, agent_loop=WebSearchAgent)
result  = await service.run(OrchestrationRequest(message=…, attachments=…))
body    = service.payload(result)      # trace clipped for the browser
```

```
app/                                     one folder, as before — services side by side
  llm/            search/               ← siblings: providers, backends
  agent.py        memory.py             ← siblings: tool loop, conversations
  orchestrator/   ← this service: service.py · contracts.py · host.py · manager…
  main.py                               ← the adapter that wires them together
  config.py       logs.py               ← host concerns (untouched)
```

To lift it into another project: copy `app/orchestrator/`, rewrite the four
functions in `host.py`, implement the three ports, wire one `OrchestratorService`.
`app/orchestrator/README.md` is the checklist.

Nothing about the algorithm changed for this: the manager, planner, executor,
agents, trace and run log are the same code, and `tests/test_orchestrator_service.py`
proves the package runs against *foreign* objects (a dataclass settings object,
a two-method model chain, an injected agent loop). It also parses the AST of
every file in the folder and fails if anything imports the host outside
`host.py` — the boundary is enforced, not just described.

### What a plan looks like

```bash
curl -s -X POST localhost:8000/orchestrate -H 'content-type: application/json' \
  -d '{"message":"Average revenue per month in the attached CSV, and what changed in the latest Python release?"}' \
  | python -m json.tool
```

```json
{
  "answer": "…the manager's written answer…",
  "mode": "auto",
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
    "review": {"asked": true, "note": "the file answer is enough", "follow_up": []},
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
| review | one small call, only when more than one agent ran | `ORCHESTRATOR_REVIEW=false` |
| wrap-up | one synthesis call | `ORCHESTRATOR_SYNTHESIZE=false` → deterministic labelled merge |

So a two-agent run costs four model calls (planner + 2 agents + review +
synthesis is five if the review is asked for) — visible in every trace entry and
in `orchestration.model_calls`.

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

- any attachment → **document**; a tabular attachment, analysis wording, or ≥3 numbers → **data**
- freshness/source wording ("latest", "news", "price", "cite", "source") → **search**
- nothing applies, or the request asks to explain/compare/design → **general**
- specialists present *and* reasoning asked for → generalist joins; a plain lookup stays one agent
- `ORCHESTRATOR_AGENTS` is the roster; an agent missing from it is never planned

Nothing breaks without a model: the document agent returns the retrieved
excerpts, the data agent returns the computed figures, and the manager merges
them with a note saying synthesis was skipped. `/chat` still fails loudly when
no provider is configured, since it has nothing to fall back on.

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
| `GET /runs` | list of recent runs (plan, agents, calls, ms, answer preview) | summaries |
| `GET /runs/{run_id}` | one full record | unclipped |
| `logs/orchestrator/runs.jsonl` | one JSON line per run | unclipped |
| `logs/orchestrator/<run-id>.json` | the same record, pretty-printed | unclipped |

```bash
python -m scripts.logs runs                  # table: run id, plan, agents, calls, ms
python -m scripts.logs runs --run <id>       # every step, with prompt/tool previews
python -m scripts.logs runs --run <id> --full  # full JSON details
curl -s localhost:8000/runs | python -m json.tool
```

The sidebar's **Run log** panel lists the same runs; clicking one loads the
stored record into the inspector, so a run can be audited after the page has
been reloaded.

### Attachments

`POST /files {"name", "data_b64"}` (the UI base64-encodes what the browser
already has, so no multipart dependency) or `{"name", "text"}`.

| Type | How |
|---|---|
| `.txt .md .csv .tsv .json .yaml .xml .html` + code | decoded as text; CSV/TSV/JSON also parsed into rows + header |
| `.docx` | Word is a zip → `word/document.xml` |
| `.xlsx` | `xl/worksheets` + `xl/sharedStrings` |
| `.pdf` | needs `pip install pypdf`; otherwise **415 with that instruction** |
| `.doc` | rejected: save as `.docx` or `.txt` |

Stored under `FILES_DIR` (`.data/files`, git-ignored): text + JSON sidecar, so an
upload survives a restart. `GET /files` lists, `DELETE /files/{id}` removes.
Limits: `FILES_MAX_MB=10`, `FILES_MAX_CHARS=200000` (truncation is reported),
`FILES_CONTEXT_CHARS=12000` of excerpts per document prompt.

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

python3 -m scripts.memory list                     # conversations in memory/
python3 -m scripts.memory show <session-id>        # you │ ai, turn by turn
python3 -m scripts.memory search refund            # grep every conversation
python3 -m scripts.memory export <id> -o chat.md   # markdown transcript
python3 -m scripts.memory forget --all             # clear memory
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

## 6. HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | playground UI |
| `GET` | `/health` | which providers/chains are ready (no secrets) |
| `GET` | `/config` | effective config summary |
| `GET` | `/providers` | every model provider + search backend, with key/model links, free tier, status |
| `GET` | `/providers/{name}/models` | live model IDs from that provider (`400` if the key is missing) |
| `POST` | `/search` | direct search (no model), optional `provider` |
| `GET` | `/search/diagnose?q=&all=` | ping each backend, per-backend pass/fail + latency |
| `POST` | `/chat` | agent loop: model + `web_search`/`fetch_url` tools, returns trace |
| `POST` | `/orchestrate` | **Auto mode**: manager plans, agents run in parallel, manager synthesises (§5) |
| `POST` | `/files` | upload an attachment (`{name, data_b64}` or `{name, text}`), returns its summary |
| `GET` | `/files` | attachments currently in the store |
| `DELETE` | `/files/{id}` | remove one attachment |
| `GET` | `/agents` | agent roster, planner mode, review/synthesis flags, readiness |
| `GET` | `/runs` | recent orchestration runs from `logs/orchestrator/` (plan, agents, model calls, timing) |
| `GET` | `/runs/{run_id}` | one run, full unclipped trace (prompts, tool calls, results) |
| `GET` | `/sessions` | conversations in memory (title, turns, last message, `?q=` filter) |
| `POST` | `/sessions` | start an empty conversation (`{title, mode}`) |
| `GET` | `/sessions/{id}` | every turn of a conversation, with the payload to replay it |
| `PATCH` | `/sessions/{id}` | rename a conversation |
| `DELETE` | `/sessions/{id}` | forget a conversation |
| `GET` | `/diagnose` | model ping + search pings + hints |
| `GET` | `/version` | app version (also in `/health` and in the page `<meta>`) |

**Every route above is also served under `/api`** (`/api/chat`, `/api/health`, …), and
`GET /api` lists them. That prefix exists because the UI addresses `/api/…`; a proxy that
adds a prefix therefore keeps working instead of turning into `{"detail":"Not Found"}`.
Unknown paths return JSON with a `hint` and the route list, and `HEAD /` is answered.

```bash
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"What is the newest Python release? Cite sources.","max_steps":4}' \
  | python -m json.tool
```

Errors are structured, never silent: `400 configuration` (missing env, with `missing` list),
`502 provider` (HTTP status + hint), `502 search` (per-backend error + hint), `415/413` for an
unreadable or oversized attachment (with a `hint`), `503` when the orchestrator is disabled,
`422` validation.

---

## 7. Layout

```
app/
  config.py            all env vars, dotenv loading, chain parsing
  llm/
    presets.py         free provider catalogue: endpoints, key/model links, limits
    openai_compat.py   the single chat-completions client (tools, errors, /models, ping)
    registry.py        settings -> providers, overrides, failover chain, descriptions
    base.py            ModelResponse / ToolCall / ProviderError
  search/
    presets.py         search catalogue: which need a key, which need a server
    providers.py       ddg, wikipedia, tavily, serper, exa, google_cse, searxng
    fetch.py           fetch_url: Jina reader -> raw HTTP fallback, SSRF guard
    registry.py        ordered chain, dedupe, empty-vs-error semantics
    base.py            SearchResult / SearchResponse / SearchError
  agent.py             tool-calling loop + trace + sources
  memory.py            harness memory: conversations in memory/ (store, replay, CLI)
  orchestrator/        ONE SERVICE: portable, talks to the app only through host.py
    service.py         entry point: OrchestratorService(settings, models=, search=, agent_loop=)
    contracts.py       ports: model chain, search chain, agent loop, settings, request
    host.py            the only file that imports the app (logger, log dir, loop, settings)
    README.md          the service contract + the porting checklist
    manager.py         classify -> plan -> waves -> review -> synthesis, totals, errors
    planner.py         rules + LLM planner, per-agent briefs, dependency graph
    executor.py        waves: asyncio.gather + semaphore + timeout, failures isolated
    agents.py          document / search / data / general workers + retrieval
    trace.py           every step of a run (prompts, tools, replies), clipping
    runlog.py          logs/orchestrator/ records: save, list, read
    files.py           upload decoding, extraction (txt/csv/json/docx/xlsx/pdf), store
  main.py              FastAPI routes (served at / and /api)
  logs.py              LOGGING: rotating files, redaction, request lines, crash capture
  static/index.html    test harness (modes, diagnostics, history, inspector)
  static/assets/       the original dark console CSS + app.js
run.py                 server launcher: preflight + banner + logs + startup record
.data/files/           uploaded attachments (runtime, git-ignored)
logs/                  runtime logs (git-ignored) + logs/changelog/ release notes
logs/orchestrator/     one JSON record per Auto run (trace, prompts, timings)
memory/                the conversations themselves: sessions/*.json + .md (git-ignored)
scripts/
  start.py             checks then server: the one command to run
  logs.py              read them: list | tail -f | errors | requests | process
  memory.py            conversations: list | show | search | export | forget
  doctor.py            model + search + optional agent, one command
  check_providers.py   table, links, --ping, --models
  check_search.py      diagnose backends, run a real query
tests/                 offline tests (MockTransport, no keys)
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `400 configuration … LLM_PROVIDER` | No provider is ready. A provider needs **both** a key and a model; the message lists exactly which env vars to fill |
| `missing GROQ_MODEL` / `no model` status | The key is fine, the model line is empty. Copy a live id: `python -m scripts.check_providers --provider groq --models` |
| `auto` picked a provider you did not want | Another provider earlier in the auto order has a key + model. Set `LLM_PROVIDER=<name>` to pin one, or `LLM_FALLBACKS=<name>` to put it first |
| `502 [groq] HTTP 429` | Free-tier rate limit. Add `LLM_FALLBACKS=gemini` or wait |
| `[gemini] … HTTP 404` / `no longer available to new users` | Retired model id. Copy a live one into `GEMINI_MODEL` (`python -m scripts.check_providers --provider gemini --models`) |
| `[openrouter] … HTTP 400/404` / `No endpoints found` | That `:free` model lost its endpoints. Pick another `:free` id for `OPENROUTER_MODEL` |
| `502 … endpoint not found` | Wrong base URL path (OpenAI-style ends with `/v1`, Gemini `/v1beta/openai`) |
| `model rejected tool calling` | Model too small for function calling. Pick a bigger one |
| chat is empty after a reload / `"session": null` | Memory is off or `MEMORY_DIR` is not writable. Check `GET /health` → `memory`, set `HARNESS_MEMORY=true`, and `python -m scripts.memory list` |
| `search … bot challenge` | DDG blocking the IP. `SEARCH_PROVIDERS=tavily,ddg` or `wikipedia,ddg` |
| `all search backends failed` | No network / all keys missing. `python -m scripts.check_search --all` |
| `SearXNG did not return JSON` | Enable `search.formats: [html, json]` in `settings.yml` |
| Ollama `network error` | `ollama serve` is not running, or `OLLAMA_BASE_URL` is wrong |
| `{"detail":"Not Found"}` on send | You were on an older page that posted to a path this app did not serve. Reload: the composer must read `· v0.5.0`. Both `/chat` and `/api/chat` answer now |
| Buttons dead, modes stacked inline | CSS/JS did not load (stale `/assets/...` URLs). Hard-reload; a red banner now says so when it happens |
| Composer shows no `· v0.5.0` | The browser cached an old page. `Ctrl+Shift+R`; the server sends `Cache-Control: no-store` |
| Anything else | `python -m scripts.logs errors` (tracebacks), `... requests --status 5xx`, `... tail -f` |

## 9. Logs

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
python3 run.py                       # writes the files above
python -m scripts.logs list          # files, sizes, last write
python -m scripts.logs tail -f       # follow the app log
python -m scripts.logs tail error -n 100
python -m scripts.logs errors        # records with tracebacks
python -m scripts.logs requests --status 5xx --slowest 5
python -m scripts.logs process       # every start: pid, port, cwd, git
```

A line looks like:

```
2026-10-31 14:27:19 WARNING  pid=1803 agetnic.chat: chat rejected, no provider: No model provider is configured. …
2026-10-31 14:27:19 WARNING  pid=1803 agetnic.requests: POST /api/chat -> 400 7.9ms req=970f99412b client=127.0.0.1:52144 bytes=245
```

The `req=` id is returned in the `x-request-id` response header and in every JSON 500, so a
number from the browser leads straight to its lines. An unhandled exception answers
`500 {"error":"internal", "request_id": …}` and writes the traceback to `error.log`.

**Secrets are redacted** before writing: env values matching `*_API_KEY`, `*_TOKEN`,
`*_SECRET`, `*_PASSWORD`, plus `sk-…`, `gsk_…`, `AIza…`, `Bearer …` and `api_key=…` shapes
become `«redacted»` (tracebacks included). Prompts/answers are not logged at `INFO`.

Tuning: `LOG_LEVEL` (default `INFO`), `LOG_ROTATE_MB` (5), `LOG_BACKUPS` (5), `LOG_DIR`.
One run can override everything: `python3 run.py --log-level DEBUG --log-dir /tmp/agetnic`.

`artifacts/doctor.json` marks checks that were not requested explicitly, e.g.
`"agent": {"skipped": true, "reason": "…"}` — nothing is `null`. The agent run only happens
with `--agent`, because it costs one model call plus one or two searches; `--search-only`
skips the model ping the same way.

`logs/changelog/` holds release notes, one markdown file per version, indexed in
[`logs/changelog/README.md`](logs/changelog/README.md) — those are notes, not logs.

## 10. Design notes

- **One dialect, many vendors.** OpenAI-compatible chat completions is the narrow waist;
  presets only carry endpoint/auth/model/link differences. Adding a provider is a dict
  entry, not a new client.
- **No mock provider.** Deleting the fake model is deliberate: a silent mock makes a broken
  key look like a working system. Missing configuration is an error with instructions.
- **Errors are data.** Tool and provider failures are returned to the model *and* to the
  caller (`errors[]`, `attempts[]`), so the agent can report a failed search truthfully.
- **Docs are tested.** `tests/test_docs.py` asserts every provider in code appears in
  `.env.example` with its env vars and links, and that no secret-looking value is committed.
- **Logs are part of the product.** A request that fails must leave a line you can find:
  status, duration, request id, client, and a traceback in `error.log` when it was a crash.
  Redaction runs on the way out, so debugging never means pasting a key into a chat.
