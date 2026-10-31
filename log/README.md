# Logs

Two kinds of log live here, both keyed by version:

| What | Path | Written by |
| --- | --- | --- |
| **Release log** (what changed, one file per version) | `log/v<version>.md` | humans, at release time |
| **Runtime log** (one line per startup/event) | `log/runtime/v<version>.log` | `python3 run.py` via `app/applog.py` |

The version string is the single source of truth in `app/main.py`
(`VERSION`) and is mirrored into the page as `<meta name="agetnic-version">`,
into the `?v=` cache-bust on every asset, and into the composer label
(`· v0.4.0`). If those disagree, the browser is showing a cached page.

## Index

| Version | Date | State | Log | Headline |
| --- | --- | --- | --- | --- |
| 0.1.0 | 2026-10-31 | merged | [v0.1.0.md](v0.1.0.md) | Provider-agnostic core, custom `.env` provider, key-free search backends |
| 0.2.0 | 2026-10-31 | merged (PR #3, `82a159e`) | [v0.2.0.md](v0.2.0.md) | PR #1 frontend restored, with diagnostics kept |
| 0.3.0 | 2026-10-31 | superseded | [v0.3.0.md](v0.3.0.md) | Intermediate API-routing rework; never shipped on its own |
| 0.4.0 | 2026-10-31 | merged (PR #4, `7846668`) | [v0.4.0.md](v0.4.0.md) | `Not Found` on `POST /chat` fixed, relative assets, launcher |

## Adding a version

1. Bump `VERSION` in `app/main.py`.
2. Bump `?v=` in `app/static/index.html` (3 assets) and the `<meta>` tag.
3. Add `log/v<new>.md` using the same sections as the existing files.
4. Add a row to the table above.
5. `python3 -m unittest discover -s tests -t .` — `tests/test_logs.py` fails if a
   release has no log file or the index forgets it.

## When the UI says "Not Found"

`Not Found` is FastAPI's own 404 body (`{"detail":"Not Found"}`), so the request
did reach a server that does not serve that path. Check, in order:

1. **Is the page served by this app?** Open `http://localhost:8000/` —
   the composer must show `· v0.4.0`. If it is missing, the browser rendered a
   stale copy from disk; hard-reload (`Ctrl+Shift+R`).
2. **Is the API on the path the UI asked for?** The error text prints
   `requested: /chat` plus a hint. Both `/chat` and `/api/chat` answer here, so a
   prefix mismatch should not happen.
3. **Is anything listening?** `curl -s localhost:8000/health` →
   `{"status":"ok",...}`. No answer means the process died; restart with
   `python3 run.py`, which now prints a preflight block and writes
   `log/runtime/v<version>.log`.
4. **Are styles/scripts loading?** A page with no CSS renders the mode buttons
   inline "alongside everything else"; that is the hardcoded-`/assets` symptom,
   fixed in 0.4.0 by relative URLs. If you saw it, you were on a cached page.
5. **Red banner at the top?** It names every probe URL it tried
   (`health → /health → api/health → /api/health`) and offers a Retry button.
