"""
backend.orchestration.router
============================

**Hand-off router** — the classic "LLM picks a mode" behaviour, kept as a
first-class component:

1. **Manual modes** — the user explicitly selects a mode; the router only
   validates the mapping mode → agent.
2. **Fallback routing** — when the Planner fails or is disabled, the
   router picks the single best agent from the goal's wording
   (keyword heuristics: deterministic, offline, testable).

The Manager treats the router as plan-B: full multi-agent planning is
preferred, but a good single-agent answer beats a broken pipeline.
"""

from __future__ import annotations

from typing import Dict

from ..schemas import AgentName, Mode

# Mode → agent mapping for the manual (explicit) modes.
MODE_TO_AGENT: Dict[Mode, AgentName] = {
    Mode.DOCUMENT: AgentName.DOCUMENT,
    Mode.WEB_SEARCH: AgentName.WEB_SEARCH,
    Mode.GENERAL: AgentName.GENERAL,
    Mode.NUMBER: AgentName.NUMBER,
}

# Ordered keyword heuristics for single-agent fallback routing.
# Order matters: specialists are checked before the general catch-all.
_KEYWORDS = [
    (AgentName.DOCUMENT, (
        "document", "file", "pdf", "upload", "according to", "my notes",
        "policy", "contract", "handbook", "manual", "transcript", "rag",
    )),
    (AgentName.WEB_SEARCH, (
        "search", "web", "internet", "online", "latest", "news", "current",
        "today", "price", "weather", "look up", "find out", "google",
        "recent", "trend", "who is",
    )),
    (AgentName.NUMBER, (
        "data", "analy", "number", "statistic", "calculat", "math",
        "compute", "metric", "dataset", "compare", "forecast", "budget",
        "revenue", "percent", "average", "kpi",
    )),
]


class Router:
    """Keyword hand-off router (deterministic, dependency-free)."""

    def agent_for_mode(self, mode: Mode) -> AgentName | None:
        """Map a manual mode to its agent (``None`` for AUTO)."""
        return MODE_TO_AGENT.get(mode)

    def choose_agent(self, goal: str) -> AgentName:
        """Pick the single best agent for a free-form goal."""
        lower = goal.lower()
        for agent, keywords in _KEYWORDS:
            if any(keyword in lower for keyword in keywords):
                return agent
        return AgentName.GENERAL

    def explain(self, goal: str) -> str:
        """Human-readable routing rationale (goes into the trace)."""
        agent = self.choose_agent(goal)
        return (
            f"Router heuristic selected '{agent.value}' for this goal "
            f"(keyword hand-off)."
        )
