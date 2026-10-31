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
# open .env, set LLM_PROVIDER + one API key (see §2)

python -m scripts.check_providers      # table + which env var is missing
python -m scripts.check_providers --provider groq --models   # live ids, best first
python -m scripts.doctor               # pings the model + every search backend
python -m scripts.check_search -q "python 3.13 release notes"

python3 run.py                    # UI + API on http://localhost:8000  (add --reload while editing)
```

`run.py` is the supported entry point: it starts **one** process that serves both the page
and the API, prints the URLs/routes, warns about a missing `.env` or missing assets, and
starts the logs described in §8 (`logs/agetnic.log`, `error.log`, `requests.log`,
`terminal.log`, `process.log`). Plain `uvicorn app.main:app --port 8000` works too — it
configures the same files on startup.

Open <http://localhost:8000> — the **test harness** (the original PR #1 UI, rebuilt on this
API): mode selector (Agent / Answer only / Search only), search-backend list, diagnostics
panel, run history, trace inspector, and a providers modal with every key/model link.

```bash
python -m unittest discover -s tests -v     # offline, no keys, no network
```

---

## 2. Choosing the core model (all free)

Set `LLM_PROVIDER` to one of these and fill the matching key. **Every option is free**
(free permanent tier, free monthly credits, or a local runtime). Two links per provider:
where to create the key, and where to see the *live* model IDs.

| `LLM_PROVIDER` | Free tier | Get an API key | Model list | Required env |
|---|---|---|---|---|
| `groq` | ~30 req/min, 14.4k req/day on 8B, no card | <https://console.groq.com/keys> | <https://console.groq.com/docs/models> | `GROQ_API_KEY` |
| `gemini` | Free tier per model, 1M context, no card | <https://aistudio.google.com/apikey> | <https://ai.google.dev/gemini-api/docs/models> | `GEMINI_API_KEY` |
| `openrouter` | ~20 `:free` models, 20 req/min, 50 req/day | <https://openrouter.ai/keys> | <https://openrouter.ai/models?max_price=0> | `OPENROUTER_API_KEY` |
| `mistral` | Free "Experiment" plan, ~2 req/min | <https://console.mistral.ai/api-keys> | <https://docs.mistral.ai/getting-started/models/models_overview/> | `MISTRAL_API_KEY` |
| `cloudflare` | 10,000 neurons/day, no card | <https://dash.cloudflare.com/profile/api-tokens> | <https://developers.cloudflare.com/workers-ai/models/> | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` |
| `nvidia` | Free developer credits, no card | <https://build.nvidia.com/> | <https://build.nvidia.com/models> | `NVIDIA_API_KEY` |
| `huggingface` | Small free monthly credits | <https://huggingface.co/settings/tokens> | <https://huggingface.co/models?inference=warm> | `HUGGINGFACE_API_KEY` |
| `ollama` | Local, unlimited, no account | <https://ollama.com/download> | <https://ollama.com/library> | `OLLAMA_MODEL` (server on `:11434`) |
| `lmstudio` | Local, unlimited, no account | <https://lmstudio.ai/> | <https://lmstudio.ai/models> | `LMSTUDIO_MODEL` (server on `:1234`) |
| `cerebras` | Free trial credits then card | <https://cloud.cerebras.ai/> | <https://inference-docs.cerebras.ai/models/overview> | `CEREBRAS_API_KEY` |
| `custom` | whatever your endpoint offers | *(your provider)* | *(your provider, or `GET /v1/models`)* | `CUSTOM_BASE_URL` + `CUSTOM_MODEL` |

### `LLM_MODEL=auto` — why there is no hard-coded default

Model ids retire constantly, and a retired id fails 100% of the time. Real examples, both
seen on 2026-10-31:

| Symptom | Provider's own message | Meaning |
|---|---|---|
| `[gemini] chat/completions failed with HTTP 404` | *"This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use models/gemini-3.8-flash"* | The endpoint and the key are fine — the **model id is retired** |
| `[openrouter] chat/completions failed with HTTP 400` (or 404) | *"No endpoints found for meta-llama/llama-3.3-70b-instruct:free"* | The id still exists in the catalogue but **has zero live endpoints** |

So the backend no longer ships a default id. With `LLM_MODEL=auto` (the default) it asks the
provider's `/models` and picks the best live model:

- **Gemini:** the rolling `gemini-flash-latest` alias first (survives renames), then the
  newest full Flash (`gemini-3.8-flash`, …), Lite only after that, images/TTS/audio/embeddings excluded.
- **OpenRouter:** only ids ending in `:free`, preferring models whose
  `supported_parameters` include `tools` (the agent needs function calling).
- **Groq:** `gpt-oss-120b` → `gpt-oss-20b` → Llama 3.3 → 8B instant.
- **Local (Ollama/LM Studio):** whatever the server actually lists.

If you *do* pin an id and it gets retired, `LLM_AUTO_SUBSTITUTE=true` (default) retries with
the next live model and reports which model answered. `LLM_MODEL_ATTEMPTS` caps the tries.

Notes that matter:

- **Never copy an id from a blog post.** `GET /providers/gemini/models` (or
  `python -m scripts.check_providers --provider gemini --models`) lists what your key can
  actually call, best first under `recommended`.
- `python -m scripts.check_providers --provider gemini --models --autofix` writes the
  recommended id into `.env` for you (backup: `.env.bak`).
- **Tool calling is required** for web search. Most Llama/Qwen/Gemini/Mistral models support
  it; tiny 1–3B models often do not, and the API will return a 400 that the error hint
  explains.
- `gemini` is wired through Google's OpenAI-compatibility endpoint
  (`/v1beta/openai`), which supports function calling — that's why one code path works.

### Failover

```dotenv
LLM_PROVIDER=groq
LLM_FALLBACKS=gemini,openrouter
```

Tried left to right; the first provider that answers wins. A 429 on Groq silently moves to
Gemini. Each fallback uses its own `<PROVIDER>_API_KEY` / `<PROVIDER>_MODEL`.
`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` override the **primary** only (useful to point a
preset at a proxy).

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

## 5. HTTP API

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
`502 provider` (HTTP status + hint), `502 search` (per-backend error + hint), `422` validation.

---

## 6. Layout

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
  main.py              FastAPI routes (served at / and /api)
  logs.py              LOGGING: rotating files, redaction, request lines, crash capture
  static/index.html    test harness (modes, diagnostics, history, inspector)
  static/assets/       the original dark console CSS + app.js
run.py                 launcher: preflight + banner + terminal mirror + startup record
logs/                  runtime logs (git-ignored) + logs/changelog/ release notes
scripts/
  logs.py              read them: list | tail -f | errors | requests | process
  doctor.py            model + search + optional agent, one command
  check_providers.py   table, links, --ping, --models
  check_search.py      diagnose backends, run a real query
tests/                 offline tests (MockTransport, no keys)
```

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `400 configuration … LLM_PROVIDER` | No key for the selected provider. `python -m scripts.check_providers --links` |
| `502 [groq] HTTP 429` | Free-tier rate limit. Add `LLM_FALLBACKS=gemini` or wait |
| `[gemini] … HTTP 404` / `no longer available to new users` | Retired model id. Set `GEMINI_MODEL=auto` (or `LLM_MODEL=auto`) |
| `[openrouter] … HTTP 400/404` / `No endpoints found` | That `:free` model lost its endpoints. Use `OPENROUTER_MODEL=auto` |
| `502 … endpoint not found` | Wrong base URL path (OpenAI-style ends with `/v1`, Gemini `/v1beta/openai`) |
| `model rejected tool calling` | Model too small for function calling. Pick a bigger one |
| `search … bot challenge` | DDG blocking the IP. `SEARCH_PROVIDERS=tavily,ddg` or `wikipedia,ddg` |
| `all search backends failed` | No network / all keys missing. `python -m scripts.check_search --all` |
| `SearXNG did not return JSON` | Enable `search.formats: [html, json]` in `settings.yml` |
| Ollama `network error` | `ollama serve` is not running, or `OLLAMA_BASE_URL` is wrong |
| `{"detail":"Not Found"}` on send | You were on an older page that posted to a path this app did not serve. Reload: the composer must read `· v0.5.0`. Both `/chat` and `/api/chat` answer now |
| Buttons dead, modes stacked inline | CSS/JS did not load (stale `/assets/...` URLs). Hard-reload; a red banner now says so when it happens |
| Composer shows no `· v0.5.0` | The browser cached an old page. `Ctrl+Shift+R`; the server sends `Cache-Control: no-store` |
| Anything else | `python -m scripts.logs errors` (tracebacks), `... requests --status 5xx`, `... tail -f` |

## 8. Logs

Everything goes to `logs/` (override with `LOG_DIR`). Files are created on start, rotate at
5 MB × 5, and are git-ignored — only `logs/README.md` and `logs/changelog/` are committed.

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

`logs/changelog/` holds release notes, one markdown file per version, indexed in
[`logs/changelog/README.md`](logs/changelog/README.md) — those are notes, not logs.

## 9. Design notes

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
