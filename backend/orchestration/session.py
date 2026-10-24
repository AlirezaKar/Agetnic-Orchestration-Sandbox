"""
backend.orchestration.session
=============================

Minimal in-memory conversation-memory store.

The test harness is multi-turn ("what about the documents?" after a first
question), so agents need the recent history.  For a sandbox, a dict keyed
by ``session_id`` is the right size — no database, easy to inspect.
"""

from __future__ import annotations

import threading
from typing import Dict, List

# Only the most recent N turns are kept per session (memory hygiene).
MAX_TURNS_PER_SESSION = 20


class SessionStore:
    """Thread-safe conversation history keyed by session id."""

    def __init__(self) -> None:
        self._sessions: Dict[str, List[Dict[str, str]]] = {}
        self._lock = threading.Lock()

    def append(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            history = self._sessions.setdefault(session_id, [])
            history.append({"role": role, "content": content})
            if len(history) > MAX_TURNS_PER_SESSION:
                del history[: len(history) - MAX_TURNS_PER_SESSION]

    def history(self, session_id: str) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


# Process-wide singleton used by the API layer.
session_store = SessionStore()
