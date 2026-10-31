# logs/

Program logs for this checkout. They are written by `app/logs.py`, configured by
`python run.py` (or by `uvicorn app.main:app` on startup) and readable with
`python -m scripts.logs`.

Files here are **generated at runtime and git-ignored** — only this README and
`changelog/` are committed.

## The files

| File | What it holds | Rotates |
| --- | --- | --- |
| `agetnic.log` | everything at `LOG_LEVEL`+ — requests, provider calls, warnings, info | 5 MB × 5 |
| `error.log` | `WARNING` and above **with full tracebacks** — start here when something fails | 5 MB × 5 |
| `requests.log` | one line per HTTP call: `POST /chat -> 400 12.4ms req=4f2a1c client=127.0.0.1:54321 bytes=318` | 5 MB × 5 |
| `terminal.log` | raw mirror of stdout/stderr, i.e. what the terminal window printed (uvicorn lines included) | 10 MB, at startup |
| `process.log` | one JSON line per start: pid, ppid, cwd, argv, python, platform, git commit, port, preflight problems | — |
| `crash.log` | `faulthandler` dump for a segfault or hard abort | — |
| `archive/` | rotated files: `agetnic.log.1`, `error.log.2`, `terminal-20261031-141702.log` | keeps the last 5 |

A normal line looks like this:

```
2026-10-31 14:31:02,123 INFO     pid=2211 agetnic.requests: GET /api/health -> 200 2.1ms req=8ac41f0e2b client=127.0.0.1:54321 bytes=214
```

`pid` is there because the reloader spawns a worker: it is how you tell two
processes apart in one file.

## Reading them

```bash
python -m scripts.logs list                 # files, sizes, last write
python -m scripts.logs tail                 # last 50 lines of agetnic.log
python -m scripts.logs tail error -n 100
python -m scripts.logs tail -f              # follow (Ctrl-C to stop)
python -m scripts.logs errors               # warning/error records + tracebacks
python -m scripts.logs requests             # status summary, slowest last
python -m scripts.logs requests --status 5xx
python -m scripts.logs process              # every start: pid, port, cwd, git
python -m scripts.logs path                 # print $LOG_DIR
```

Plain `tail -f logs/agetnic.log` works as well — the files are ordinary text.

## Configuration (`.env` / environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOG_DIR` | `<repo>/logs` | where the files go (created if missing) |
| `LOG_LEVEL` | `INFO` | `DEBUG` for provider payloads, `WARNING` for a quiet run |
| `LOG_ROTATE_MB` | `5` | size that triggers rotation |
| `LOG_BACKUPS` | `5` | how many rotated copies to keep |

`python run.py --log-dir /tmp/agetnic --log-level DEBUG` overrides both for one
run; `--no-terminal-log` keeps the terminal mirror off (useful in Docker).

## What is (and is not) logged

- Every request: method, path, status, duration, request id, client, bytes. The
  same id is returned in the `x-request-id` response header, so a number from a
  browser error dialog leads straight to its lines.
- Every provider/search failure the API turns into a `4xx`/`5xx`, with the
  provider name and the exception text.
- Every unhandled exception, with a traceback, plus a JSON 500 that names the
  request id.
- Startup and shutdown, with process data.

**Secrets are redacted** before writing: values of any env var matching
`*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, plus `sk-…`, `gsk_…`, `AIza…`,
`Bearer …` and `api_key=…` shapes become `«redacted»`. Prompts and answers are
not logged at `INFO`; set `LOG_LEVEL=DEBUG` if you need that while debugging.

## `changelog/`

Release notes, one markdown file per version, committed: `v0.1.0.md` …
`v0.5.0.md`, indexed in [`changelog/README.md`](changelog/README.md). These are
*not* logs — they explain what changed between versions and how to verify it.
