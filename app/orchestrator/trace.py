"""Everything that happens during one orchestration run, in order.

The point of this module is that the answer can be *audited*: what the manager
was told, which agents it picked, the exact prompt each agent received, every
tool call and tool result, what each agent answered, what the manager did with
those answers, and the prompt of the final synthesis.

Events are kept unclipped in memory (a run is at most a few hundred kB) and
clipped only when they are serialised for the browser. The log file gets the
full text — that is where you go when the UI truncates something.
"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Stages, in the order the UI paints them.
STAGES = ("run", "manager", "planner", "agent", "tool", "synthesis", "merge", "done")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return f"run-{_dt.datetime.now(_dt.timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def clip(text: Any, limit: int | None) -> Any:
    """Trim a string for transport and say how much was cut."""
    if limit is None or not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} more chars — full text in logs/orchestrator/runs.jsonl]"


def clip_event(event: dict[str, Any], limit: int | None) -> dict[str, Any]:
    """Clip every string inside an event, recursively."""
    if limit is None:
        return event
    if isinstance(event, str):
        return clip(event, limit)
    if isinstance(event, list):
        return [clip_event(item, limit) for item in event]
    if isinstance(event, dict):
        return {key: clip_event(value, limit) for key, value in event.items()}
    return event


@dataclass
class RunTrace:
    """Ordered record of one run. ``add`` returns the event so callers can amend it."""

    run_id: str = field(default_factory=new_run_id)
    started_at: str = field(default_factory=_now)
    events: list[dict[str, Any]] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)
    _seq: int = 0

    # ---------------------------------------------------------------- writing
    def add(self, stage: str, actor: str, title: str, **detail: Any) -> dict[str, Any]:
        self._seq += 1
        event: dict[str, Any] = {
            "seq": self._seq,
            "stage": stage,
            "actor": actor,
            "title": title,
            "at": _now(),
            "offset_ms": int((time.perf_counter() - self._t0) * 1000),
            "detail": detail,
        }
        self.events.append(event)
        return event

    def add_prompt(
        self,
        stage: str,
        actor: str,
        title: str,
        messages: list[dict[str, Any]],
        **detail: Any,
    ) -> dict[str, Any]:
        """A model call: the exact conversation that was sent."""
        return self.add(
            stage,
            actor,
            title,
            messages=[
                {"role": message.get("role", "?"), "content": message.get("content", "")}
                for message in messages
            ],
            **detail,
        )

    def add_response(
        self, stage: str, actor: str, title: str, response: Any, **detail: Any
    ) -> dict[str, Any]:
        return self.add(
            stage,
            actor,
            title,
            content=getattr(response, "content", "") or "",
            tool_calls=[
                {"name": call.name, "arguments": call.arguments}
                for call in (getattr(response, "tool_calls", None) or [])
            ],
            usage=getattr(response, "usage", None) or {},
            finish_reason=getattr(response, "finish_reason", None),
            model=getattr(response, "model", "") or "",
            provider=getattr(response, "provider", "") or "",
            **detail,
        )

    # -------------------------------------------------------------- reading
    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def to_list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            return list(self.events)
        return [clip_event(event, limit) for event in self.events]

    def summary(self) -> dict[str, Any]:
        agents = [event["actor"] for event in self.events if event["stage"] == "agent"]
        tools = [
            event["detail"].get("tool")
            for event in self.events
            if event["stage"] == "tool" and event["detail"].get("kind") == "call"
        ]
        return {
            "agents": sorted({agent for agent in agents}),
            "tool_calls": [tool for tool in tools if tool],
            "events": len(self.events),
        }
