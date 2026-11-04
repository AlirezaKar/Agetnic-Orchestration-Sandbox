"""A live view of one run: the bus that carries trace events while they happen.

The pipeline already writes a full trace — but only the browser receives it at
the *end* of the request, which is why an Auto run looks frozen for ten seconds
and then produces an answer out of nowhere. This module, plus
``live.py`` next to it, is what lets the page watch the run instead:

```
Manager.run(sink=…)                    POST /orchestrate/stream
   │                                         ▲
   │  trace.add(...) ──► RunBus.publish ─┐   │  text/event-stream
   ▼                                     │   │
RunTrace.add ──► sink(event) ────────────┴──►│ Channel.queue ──► generator
```

Design notes:

* **In-process.** The bus is a dictionary of channels in this process. That is
  exactly right for the single uvicorn worker this sandbox ships, and it is
  documented as the seam to replace (Redis, NATS) before scaling out.
* **Nothing blocks.** ``publish`` never awaits: it appends to the channel's
  queue and returns, so a slow browser can never slow the orchestrator down.
  A queue is capped; if nobody is reading, the oldest events are dropped and
  the stream tells the page it missed some (``dropped`` on the next event).
* **Nothing leaks.** Channels are closed by the endpoint when the run ends, and
  :meth:`RunBus.prune` drops abandoned ones (a client that vanished mid-run).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

#: sentinel pushed into the queue when a run is finished
END = {"__end__": True}


@dataclass
class Channel:
    """One run's live event stream."""

    run_id: str
    queue_size: int = 512
    history_size: int = 512
    events: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    closed: bool = False
    started: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def __post_init__(self) -> None:
        # asyncio.Queue cannot be bounded through a dataclass default expression
        self.queue = asyncio.Queue(maxsize=self.queue_size)

    # ---------------------------------------------------------------- write
    def publish(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        self.touched = time.monotonic()
        if len(self.events) < self.history_size:
            self.events.append(event)
        if self.queue.full():
            # nobody is reading: drop the oldest so the run never blocks
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - race, harmless
                pass
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - raced with the check above
            self.dropped += 1

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.touched = time.monotonic()
        try:
            self.queue.put_nowait(END)
        except asyncio.QueueFull:  # pragma: no cover - the reader will time out
            pass

    # ----------------------------------------------------------------- read
    async def next(self, timeout: float = 60.0) -> dict[str, Any] | None:
        """The next event, ``None`` on timeout (so the caller can send a ping)."""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class RunBus:
    """Every live channel this process knows about, keyed by run id."""

    def __init__(self, *, ttl: float = 900.0, channels: int = 64) -> None:
        self._channels: dict[str, Channel] = {}
        self.ttl = ttl
        self.max_channels = channels

    # ------------------------------------------------------------------ api
    def open(self, run_id: str, **kwargs: Any) -> Channel:
        channel = Channel(run_id=run_id, **kwargs)
        self._channels[run_id] = channel
        # prune *after* inserting: the cap must never evict the run we just opened
        self.prune()
        return channel

    def get(self, run_id: str) -> Channel | None:
        return self._channels.get(run_id)

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        channel = self._channels.get(run_id)
        if channel is not None:
            channel.publish(event)

    def close(self, run_id: str) -> None:
        channel = self._channels.get(run_id)
        if channel is not None:
            channel.close()

    def forget(self, run_id: str) -> None:
        self._channels.pop(run_id, None)

    # --------------------------------------------------------------- upkeep
    def prune(self, now: float | None = None) -> int:
        """Drop finished channels past their TTL, and the oldest overflow ones."""
        now = time.monotonic() if now is None else now
        gone = [
            run_id
            for run_id, channel in self._channels.items()
            if channel.closed and now - channel.touched > self.ttl
        ]
        for run_id in gone:
            self._channels.pop(run_id, None)
        while len(self._channels) > self.max_channels:
            oldest = min(self._channels.items(), key=lambda item: item[1].started)
            self._channels.pop(oldest[0], None)
            gone.append(oldest[0])
        return len(gone)

    def __len__(self) -> int:
        return len(self._channels)


#: the process-wide bus the HTTP layer and the manager share
BUS = RunBus()
