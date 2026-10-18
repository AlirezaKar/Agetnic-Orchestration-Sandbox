"""
backend.orchestration.planner
=============================

**Planner** — turns a goal (plus the Manager's analysis) into a
structured, dependency-aware :class:`TaskPlan`.

The Planner LLM is instructed to answer with a strict JSON schema::

    {
      "analysis": "why this decomposition",
      "tasks": [
        {"id": "t1", "agent": "web_search",
         "description": "…", "depends_on": [], "reason": "…"}
      ]
    }

Robustness (because production LLMs are imperfect):
1. ``json_mode`` is requested when the provider supports it.
2. The first balanced ``{…}`` block is extracted if the answer is chatty.
3. On any failure the Planner degrades to a single General-agent task —
   the Manager may still re-plan, and the failure is captured in the trace.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from ..config import settings
from ..llm import BaseLLMClient, LLMMessage, LLMResponse
from ..schemas import AgentName, TaskPlan, TaskSpec

# ──────────────────────────────────────────────────────────────────
#  Prompt
# ──────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the Planner of a multi-agent orchestration system.

Your job: decompose the user's goal into a small set of concrete tasks, each
executed by exactly one specialist agent. Think about what must be done and
in which order — then output ONLY a JSON object (no markdown, no commentary).

## AVAILABLE AGENTS
- general:     open-domain conversation, writing, explanations, general knowledge
- web_search:  live web search for fresh/external information
- document:    answering from uploaded documents (RAG)
- number:      quantitative analysis, statistics, math, data reasoning

## PLANNING RULES
1. Only use agents that are genuinely needed to reach the goal.
2. Independent tasks must NOT depend on each other (they will run in parallel).
3. Use "depends_on" when a task consumes another task's findings
   (e.g. analysing search results → depends on the search task).
4. Prefer 1-4 tasks. Quality of decomposition beats quantity.
5. Write each "description" as a self-contained instruction (the agent sees
   only that text plus results of its dependencies).

## OUTPUT FORMAT (strict JSON)
{
  "analysis": "<short reasoning about the goal and the chosen decomposition>",
  "tasks": [
    {
      "id": "t1",
      "agent": "general | web_search | document | number",
      "description": "<self-contained instruction for the agent>",
      "depends_on": ["t0"],
      "reason": "<why this task helps reach the goal>"
    }
  ]
}"""


class Planner:
    """LLM-backed goal → task-plan compiler."""

    def __init__(self, llm: BaseLLMClient) -> None:
        self._llm = llm

    async def create_plan(
        self,
        goal: str,
        manager_analysis: str = "",
        allowed_agents: Optional[List[AgentName]] = None,
    ) -> tuple[TaskPlan, LLMResponse]:
        """
        Compile ``goal`` into a validated :class:`TaskPlan`.

        Returns the plan *and* the raw LLM response (so the caller can put
        the untouched model text into the trace).
        """
        prompt = self._build_prompt(goal, manager_analysis, allowed_agents)
        response = await self._llm.complete(
            [
                LLMMessage("system", SYSTEM_PROMPT),
                LLMMessage("user", prompt),
            ],
            json_mode=True,
        )

        try:
            plan = self._parse_plan(response.content, allowed_agents)
        except (json.JSONDecodeError, ValueError):
            # Last-resort fallback keeps the pipeline alive.
            plan = self._fallback_plan(goal)

        return plan, response

    # ── prompt assembly ─────────────────────────────────────────
    @staticmethod
    def _build_prompt(
        goal: str,
        manager_analysis: str,
        allowed_agents: Optional[List[AgentName]],
    ) -> str:
        parts = [f"## USER GOAL\n{goal}"]
        if manager_analysis:
            parts.append(f"## MANAGER ANALYSIS\n{manager_analysis}")
        if allowed_agents:
            names = ", ".join(a.value for a in allowed_agents)
            parts.append(f"## CONSTRAINT: USE ONLY THESE AGENTS\n{names}")
        return "\n\n".join(parts)

    # ── parsing / validation ────────────────────────────────────
    @staticmethod
    def _extract_json_block(text: str) -> dict:
        """Parse a JSON object out of a possibly-chatty LLM answer."""
        text = text.strip()
        # Fast path: the whole answer is JSON.
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        # Slow path: first balanced {...} block.
        start = text.find("{")
        if start == -1:
            raise ValueError("no JSON object found in planner answer")
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
        raise ValueError("unbalanced JSON object in planner answer")

    def _parse_plan(
        self,
        raw: str,
        allowed_agents: Optional[List[AgentName]],
    ) -> TaskPlan:
        data = self._extract_json_block(raw)

        tasks_raw = data.get("tasks", [])
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise ValueError("planner returned no tasks")

        allowed = set(allowed_agents) if allowed_agents else None
        specs: List[TaskSpec] = []
        for item in tasks_raw[: settings.max_plan_tasks]:
            spec = TaskSpec.model_validate(item)
            if allowed is not None and spec.agent not in allowed:
                continue  # silently drop forbidden agents
            specs.append(spec)

        if not specs:
            raise ValueError("all planned tasks were filtered out")

        plan = TaskPlan(analysis=str(data.get("analysis", "")), tasks=specs)
        # Raises on cyclic/dangling deps — caught by the caller → fallback.
        plan.waves()
        return plan

    @staticmethod
    def _fallback_plan(goal: str) -> TaskPlan:
        """
        Minimal one-task plan used when the LLM output is unusable.

        Routing to the right single agent is better than failing outright;
        the Manager records the planner failure in the trace either way.
        """
        from .router import Router  # local import avoids a cycle

        agent = Router().choose_agent(goal)
        return TaskPlan(
            analysis="Planner output was invalid — fell back to single-agent routing.",
            tasks=[
                TaskSpec(
                    id="t1",
                    agent=agent,
                    description=goal,
                    depends_on=[],
                    reason="Fallback route chosen by keyword heuristics.",
                )
            ],
        )
