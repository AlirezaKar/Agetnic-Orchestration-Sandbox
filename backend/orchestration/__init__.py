"""
backend.orchestration — the coordination layer.

``manager``   root brain: analyse → plan → delegate → supervise → synthesise
``planner``   goal → dependency-aware task plan (LLM + robust parsing)
``executor``  parallel wave execution of task plans
``router``    classic hand-off routing (manual modes + fallback)
``trace``     run recorder → traces/<run_id>.json (evaluation & debugging)
``session``   in-memory conversation memory for multi-turn chat
"""

from .manager import Manager
from .planner import Planner
from .executor import ParallelExecutor
from .router import Router
from .session import session_store
from .trace import TraceRecorder, list_trace_summaries, load_trace

__all__ = [
    "Manager",
    "Planner",
    "ParallelExecutor",
    "Router",
    "session_store",
    "TraceRecorder",
    "list_trace_summaries",
    "load_trace",
]
