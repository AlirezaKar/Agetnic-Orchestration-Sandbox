"""
backend.orchestration.trace
===========================

**Trace recorder** — the evaluation & debugging backbone of the sandbox.

Every orchestrated run produces exactly one JSON file
(``traces/<run_id>.json``) containing *everything* needed to study the
run afterwards:

* the raw user **input**
* every **thinking** block (Manager analysis, Planner analysis, agent reasoning)
* the complete **process** (task plan, per-task I/O, timings, waves, errors)
* the final **output**
* a redacted config snapshot + metrics

A one-line summary is appended to ``traces/index.jsonl`` so runs can be
listed cheaply without opening each file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

from ..config import settings
from ..schemas import RunTrace, TaskPlan, TraceStep, utc_now_iso

# Writes happen from async code; a small lock keeps the index file
# consistent even with concurrent requests.
_INDEX_LOCK = threading.Lock()


class TraceRecorder:
    """
    Accumulates one :class:`RunTrace` and persists it as JSON.

    Usage::

        recorder = TraceRecorder(trace)
        recorder.add_thinking("…")
        recorder.set_plan(plan)
        recorder.add_step(step)
        path = recorder.save()
    """

    def __init__(self, trace: RunTrace) -> None:
        self.trace = trace

    # ── accumulate ──────────────────────────────────────────────
    def add_thinking(self, text: str) -> None:
        """Append a thinking/reasoning block (Manager, Planner, …)."""
        if text:
            self.trace.thinking.append(text)

    def set_plan(self, plan: TaskPlan) -> None:
        """Record the current task plan (also kept in the final file)."""
        self.trace.plan = plan

    def add_step(self, step: TraceStep) -> None:
        """Append a timeline step (agent task, synthesis, …)."""
        self.trace.steps.append(step)

    def replace_steps_for_replan(self) -> None:
        """
        Keep old steps but mark the trace as re-planned.

        Steps are never deleted — evaluation wants the full history of
        what was attempted, including failed approaches.
        """
        self.trace.replan_count += 1

    # ── persist ─────────────────────────────────────────────────
    def _index_entry(self) -> dict:
        agents_used = sorted(
            {
                step.data.get("agent", "")
                for step in self.trace.steps
                if step.data.get("agent")
            }
        )
        return {
            "run_id": self.trace.run_id,
            "session_id": self.trace.session_id,
            "mode_requested": self.trace.mode_requested.value,
            "mode_used": self.trace.mode_used.value,
            "created_at": self.trace.created_at,
            "finished_at": self.trace.finished_at,
            "success": self.trace.success,
            "input_preview": self.trace.input[:120],
            "output_preview": self.trace.output[:120],
            "steps": len(self.trace.steps),
            "replan_count": self.trace.replan_count,
            "agents_used": [a for a in agents_used if a],
            "duration_ms": self.trace.metrics.get("total_duration_ms", 0),
        }

    def save(self) -> Path:
        """
        Write ``traces/<run_id>.json`` and append to ``traces/index.jsonl``.

        Returns the path of the run file (relative to the repo root when
        possible, so the UI can display a tidy filename).
        """
        if not self.trace.finished_at:
            self.trace.finished_at = utc_now_iso()

        trace_dir: Path = settings.trace_dir
        trace_dir.mkdir(parents=True, exist_ok=True)

        run_path = trace_dir / f"{self.trace.run_id}.json"
        run_path.write_text(
            json.dumps(self.trace.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        index_path = trace_dir / "index.jsonl"
        entry = json.dumps(self._index_entry(), ensure_ascii=False)
        with _INDEX_LOCK, index_path.open("a", encoding="utf-8") as fh:
            fh.write(entry + "\n")

        return run_path

    def save_relative(self) -> str:
        """Save and return a repo-relative path string for the API/UI."""
        path = self.save()
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return str(path)


def load_trace(run_id: str) -> Optional[RunTrace]:
    """Load one persisted run trace by id (``None`` if unknown)."""
    path = settings.trace_dir / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return RunTrace.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def list_trace_summaries(limit: int = 50) -> List[dict]:
    """Return the most recent index entries (newest first)."""
    index_path = settings.trace_dir / "index.jsonl"
    if not index_path.exists():
        return []
    entries: List[dict] = []
    with index_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip corrupted lines rather than failing listing
    entries.reverse()  # newest first
    return entries[:limit]
