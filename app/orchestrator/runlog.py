"""The orchestration log base: one JSON record per run, kept on disk.

```
logs/orchestrator/runs.jsonl        every run, one line each (newest last)
logs/orchestrator/<run-id>.json    the same record, full trace, one file per run
```

"Full" means unclipped: prompts, tool arguments, tool results and answers as the
model saw them. The HTTP response clips long strings for the browser; this file
does not, so it is the place to check when the UI says "…more chars".

Read it with:

    python -m scripts.logs runs            # table of recent runs
    python -m scripts.logs runs --run <id> # full trace of one run
    GET /runs            GET /runs/{id}    # same over HTTP
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app import logs

RUN_LOG_NAME = "runs.jsonl"


def runs_dir(log_dir: str | os.PathLike[str] | None = None) -> Path:
    path = Path(logs.log_dir(log_dir)) / "orchestrator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rotate(path: Path, max_mb: float) -> None:
    try:
        if path.exists() and path.stat().st_size > max_mb * 1024 * 1024:
            backup = path.with_suffix(".1.jsonl")
            path.replace(backup)
    except OSError:  # pragma: no cover - log volume problems never break a run
        pass


def summary_of(record: dict[str, Any]) -> dict[str, Any]:
    """The small part of a run that a list view needs."""
    orchestration = record.get("orchestration") or {}
    plan = orchestration.get("plan") or {}
    return {
        "run_id": record.get("run_id", ""),
        "at": record.get("at", ""),
        "message": (record.get("message") or "")[:300],
        "intent": plan.get("intent", ""),
        "agents": plan.get("agents", []),
        "strategy": plan.get("strategy", ""),
        "synthesized": bool(orchestration.get("synthesized")),
        "status": record.get("status", "ok"),
        "latency_ms": record.get("latency_ms", 0),
        "parallel_ms": orchestration.get("parallel_ms", 0),
        "model_calls": orchestration.get("model_calls", 0),
        "tool_calls": (record.get("trace_summary") or {}).get("tool_calls", []),
        "errors": len(record.get("errors") or []),
        "answer": (record.get("answer") or "")[:400],
    }


def save_run(record: dict[str, Any], *, log_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """Append to ``runs.jsonl`` and write ``<run-id>.json``. Never raises."""
    directory = runs_dir(log_dir)
    run_id = str(record.get("run_id") or "run-unknown")
    try:
        _rotate(directory / RUN_LOG_NAME, float(os.environ.get("LOG_ROTATE_MB", "5")))
        line = json.dumps(record, ensure_ascii=False)
        with (directory / RUN_LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        target = directory / f"{run_id}.json"
        target.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return target
    except OSError as exc:  # pragma: no cover - read-only checkouts still work
        logs.get_logger("orchestrator").warning("could not write the run log: %s", exc)
        return None


def list_runs(
    limit: int = 20, *, log_dir: str | os.PathLike[str] | None = None
) -> list[dict[str, Any]]:
    path = runs_dir(log_dir) / RUN_LOG_NAME
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-500:]):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(summary_of(json.loads(line)))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def get_run(run_id: str, *, log_dir: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
    if not safe or safe != run_id:
        return None
    path = runs_dir(log_dir) / f"{safe}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover
        return None
