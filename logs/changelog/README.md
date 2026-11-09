# Changelogs

Release notes, one file per version. Runtime logs are **not** here — they live in
[`../`](../README.md) (`logs/agetnic.log`, `error.log`, `requests.log`,
`terminal.log`, `process.log`).

| What | Path | Written by |
| --- | --- | --- |
| **Release notes** (what changed, one file per version) | `logs/changelog/v<version>.md` | humans, at release time |
| **Runtime logs** | `logs/*.log` | `python3 run.py` / `app/logs.py` |

The version string is the single source of truth in `app/main.py`
(`app/version.py`, re-exported as `app.main.VERSION`) and is mirrored into the page as `<meta name="agetnic-version">`,
into the `?v=` cache-bust on every asset, and into the composer label
(`· v0.14.0`). If those disagree, the browser is showing a cached page.

## Index

| Version | Date | State | Log | Headline |
| --- | --- | --- | --- | --- |
| 0.1.0 | 2026-10-31 | merged | [v0.1.0.md](v0.1.0.md) | Provider-agnostic core, custom `.env` provider, key-free search backends |
| 0.2.0 | 2026-10-31 | merged (PR #3, `82a159e`) | [v0.2.0.md](v0.2.0.md) | PR #1 frontend restored, with diagnostics kept |
| 0.3.0 | 2026-10-31 | superseded | [v0.3.0.md](v0.3.0.md) | Intermediate API-routing rework; never shipped on its own |
| 0.4.0 | 2026-10-31 | merged (PR #4, `7846668`) | [v0.4.0.md](v0.4.0.md) | `Not Found` on `POST /chat` fixed, relative assets, launcher |
| 0.5.0 | 2026-10-31 | merged (PR #6, `9547b54`) | [v0.5.0.md](v0.5.0.md) | Real program logs: errors+tracebacks, requests, terminal mirror, process data |
| 0.6.0 | 2026-10-31 | merged (PR #8, `1b50f80`) | [v0.6.0.md](v0.6.0.md) | Lean `.env`, `LLM_PROVIDER=auto` + explicit models, `scripts.start`, no nulls in doctor JSON |
| 0.7.0 | 2026-10-31 | merged (PR #10, `a02e3fa`) | [v0.7.0.md](v0.7.0.md) | Manager orchestration: planner → parallel document/search/data/general agents → synthesis, attachments |
| 0.8.0 | 2026-10-31 | merged (PR #12, `c19ab7f`) | [v0.8.0.md](v0.8.0.md) | Full step trace (prompts, tools, replies), LLM-written briefs, dependency waves, manager review, `logs/orchestrator/` |
| 0.9.0 | 2026-11-01 | merged (PR #14, `f2c52d7`) | [v0.9.0.md](v0.9.0.md) | Harness memory: conversations stored in `memory/`, reopened and continued with their traces, `/sessions` API + Memory panel |
| 0.10.0 | 2026-11-01 | merged (PR #16, `db10169`) | [v0.10.0.md](v0.10.0.md) | Orchestrator reorganised as a portable service: `service.py` + `contracts.py` (ports) + `host.py` (single seam), boundary enforced by tests |
| 0.11.0 | 2026-11-01 | merged (PR #18, `23244fa`) | [v0.11.0.md](v0.11.0.md) | Run records always serialise (`PlanStep` 500 fixed), `/favicon.ico` 204, file permission prompt (`FILES_PERMISSION_PROMPT`, allow/deny endpoint, 403 guard) |
| 0.11.1 | 2026-11-01 | merged (PR #20, `0b4ad53`) | [v0.11.1.md](v0.11.1.md) | Sidebar tidied (5 panels, Runs tabs, diagnostics only in the top bar) and every column scrolls inside the screen instead of stretching the page |
| 0.12.0 | 2026-11-01 | merged (PR #22, `42192f5`) | [v0.12.0.md](v0.12.0.md) | Studio redesign: three-column shell, status pills, sidebar with sessions/runs/agents/search, a Markdown answer surface (tables, code, callouts), plan chips, tabbed inspector (Trace/Context/JSON) |
| 0.13.0 | 2026-11-03 | merged (PR #24, `56f652c`) | [v0.13.0.md](v0.13.0.md) | Light theme (`theme.css`, pre-paint switch, stored choice), wider columns with a lifted type scale, and drag-to-resize handles on both side columns that remember their width |
| 0.14.0 | 2026-11-04 | merged (PR #26, `1dc85de`) | [v0.14.0.md](v0.14.0.md) | Evaluator–optimizer stage between the agents and the manager: every report scored 1–5 with issues and gaps, weak ones rewritten (evidence-preserving), the manager reviewing the tightened set and asking for another wave that comes back through the gate |
| 0.15.0 | 2026-11-04 | merged (PR #28, `157ea7c`) | [v0.15.0.md](v0.15.0.md) |
| 0.15.1 | 2026-11-04 | merged (PR #30, `3027d8d`) | [v0.15.1.md](v0.15.1.md) | Fix: the live projection assumed `tool_calls` was always a list, so an agent that actually called a tool 500'd the stream; collections are now shape-checked and a failing thought degrades to a plain line instead of ending the run | Live thoughts: `POST /orchestrate/stream` relays the trace as it is written (planner, each agent, evaluator, manager) and the chat draws it as a group per actor, each line opening to the real prompt/reply/tool/verdict, with a spinner on whoever is working — the answer still arrives once |
| 0.17.0 | 2026-11-08 | open (PR pending) | [v0.17.0.md](v0.17.0.md) | The ARAS wiring: one `.env` block points the whole pipeline at the other app's microservices — router :8101 leads the model chain (OpenAI-compatible, trailing-slash aware), web :8108 leads the search chain, OCR :8102 reads PDFs with `pypdf` as fallback, retrieval :8105 becomes the document agent's knowledge store (RAG, new `KnowledgeStore` port), data :8107 becomes the data agent's analytics backend (new `DataBackend` port), ingestion :8104 optionally indexes uploads; `GET /aras` checks every service's health in one call, and nothing changes until a URL + token pair exists |
| 0.16.1 | 2026-11-08 | merged (PR #34, `0775e6c`) | [v0.16.1.md](v0.16.1.md) | Patch: `live.planner` crashed on a real key (`ValueError: too many values to unpack`) — the golden set grew a `must_not` column and the live check still unpacked three; it now scores coverage and contradictions like `plan.selection`, warns when every model reply fell back to the rules, and the live path runs offline in CI through a scripted planner so a keyless suite can never miss it again |
| 0.16.0 | 2026-11-07 | merged (PR #32, `295eabf`) | [v0.16.0.md](v0.16.0.md) | Self-audit: eighteen checks over the orchestration itself (selection, briefs, waves, isolation, timeouts, the evaluator gate, review, record, stream, grounding, repeatability, odd input) run offline through the ports in about a third of a second, one at a time, with an **Evaluate** button that fills a checklist row per check and hands back one scorecard as rows and as JSON (`logs/audit/`, `python -m scripts.audit`) |

## Adding a version

1. Bump `VERSION` in `app/main.py`.
2. Bump `?v=` in `app/static/index.html` (4 assets) and the `<meta>` tag.
3. Add `logs/changelog/v<new>.md` using the same sections as the existing files.
4. Add a row to the table above.
5. `python3 -m unittest discover -s tests -t .` — `tests/test_logs.py` fails if a
   version has no changelog or the index forgets it.

## When the UI says "Not Found"

`Not Found` is FastAPI's own 404 body (`{"detail":"Not Found"}`), so the request
did reach a server that does not serve that path. Check, in order:

1. **Is the page served by this app?** Open `http://localhost:8000/` —
   the composer must show `· v0.5.0`. If it is missing, the browser rendered a
   stale copy from disk; hard-reload (`Ctrl+Shift+R`).
2. **Is the API on the path the UI asked for?** The error text prints
   `requested: /chat` plus a hint. Both `/chat` and `/api/chat` answer here, so a
   prefix mismatch should not happen.
3. **Is anything listening?** `curl -s localhost:8000/health` →
   `{"status":"ok",...}`. No answer means the process died; restart with
   `python3 run.py`, which now prints a preflight block and writes
   `log/runtime/v<version>.log`.
4. **Check the logs.** `python -m scripts.logs errors` shows the tracebacks,
   `python -m scripts.logs requests --status 5xx` the failing calls, and
   `python -m scripts.logs process` the starts (pid, port, git commit).
5. **Are styles/scripts loading?** A page with no CSS renders the mode buttons
   inline "alongside everything else"; that is the hardcoded-`/assets` symptom,
   fixed in 0.4.0 by relative URLs. If you saw it, you were on a cached page.
6. **Red banner at the top?** It names every probe URL it tried
   (`health → /health → api/health → /api/health`) and offers a Retry button.
