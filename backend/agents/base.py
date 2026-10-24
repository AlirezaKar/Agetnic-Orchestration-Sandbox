"""
backend.agents.base
===================

Common contract for every worker agent (Document, Web-search, General,
Number).

Each agent receives a **task instruction** (produced by the Planner) plus
shared context (conversation history, retrieved documents, upstream task
results) and returns an :class:`AgentResult` whose ``thinking`` and
``data`` feed straight into the run trace.

Agents never talk to each other — only the Manager coordinates them.
That keeps every agent independently testable and swappable.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..llm import BaseLLMClient
from ..schemas import AgentName, AgentResult


@dataclass
class AgentContext:
    """
    Everything an agent may need beyond its own task description.

    * ``history``          — conversation so far (for follow-up questions)
    * ``upstream_results`` — outputs of tasks this task depends on
    * ``shared``           — free-form bag (document store refs, etc.)
    """

    history: List[Dict[str, str]] = field(default_factory=list)
    upstream_results: Dict[str, AgentResult] = field(default_factory=dict)
    shared: Dict[str, Any] = field(default_factory=dict)

    def upstream_text(self) -> str:
        """Render upstream results as text for prompt injection."""
        if not self.upstream_results:
            return ""
        blocks = []
        for task_id, result in self.upstream_results.items():
            blocks.append(f"[{task_id} → {result.agent.value}]\n{result.output}")
        return "\n\n".join(blocks)

    def history_text(self) -> str:
        """Render recent conversation turns (user/assistant) as text."""
        lines = []
        for turn in self.history[-6:]:
            role = turn.get("role", "user").upper()
            lines.append(f"{role}: {turn.get('content', '')}")
        return "\n".join(lines)


class BaseAgent(abc.ABC):
    """
    Worker-agent interface.

    Subclasses set ``name`` and implement :meth:`_execute`; the template
    method :meth:`run` wraps execution with timing + error capture so the
    trace always gets a well-formed :class:`AgentResult`.
    """

    name: AgentName
    description: str = ""

    def __init__(self, llm: BaseLLMClient) -> None:
        self._llm = llm

    @abc.abstractmethod
    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        """Do the actual work. Raise on hard failure."""

    async def run(self, task: str, context: AgentContext) -> AgentResult:
        """Execute with timing and uniform error handling."""
        started = time.perf_counter()
        try:
            result = await self._execute(task, context)
        except Exception as exc:  # noqa: BLE001 — agent errors become trace data
            result = AgentResult(
                agent=self.name,
                success=False,
                output="",
                error=f"{type(exc).__name__}: {exc}",
            )
        result.agent = self.name
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result
