"""
tests.test_planner — task-plan compilation, validation, fallback.
"""

import json

from backend.llm.base import LLMMessage
from backend.orchestration.planner import Planner
from backend.schemas import AgentName


class ScriptedLLM:
    """Minimal LLM stub that returns a canned answer (any provider shape)."""

    provider = "scripted"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list = []

    async def complete(self, messages, **kwargs):
        from backend.llm.base import LLMResponse

        self.calls.append(messages)
        return LLMResponse(content=self.content, model="scripted", provider="scripted")


def _plan_json(tasks):
    return json.dumps({"analysis": "test analysis", "tasks": tasks})


async def test_planner_parses_valid_plan():
    raw = _plan_json([
        {"id": "t1", "agent": "web_search", "description": "search stuff", "depends_on": [], "reason": "r"},
        {"id": "t2", "agent": "number", "description": "analyze stuff", "depends_on": ["t1"], "reason": "r"},
    ])
    planner = Planner(ScriptedLLM(raw))
    plan, _ = await planner.create_plan("goal")
    assert len(plan.tasks) == 2
    assert plan.tasks[1].depends_on == ["t1"]
    waves = plan.waves()
    assert [[t.id for t in w] for w in waves] == [["t1"], ["t2"]]


async def test_planner_extracts_json_from_chatty_answer():
    raw = "Sure! Here is the plan:\n```json\n" + _plan_json([
        {"id": "t1", "agent": "general", "description": "answer", "depends_on": [], "reason": "r"},
    ]) + "\n```\nGood luck."
    planner = Planner(ScriptedLLM(raw))
    plan, _ = await planner.create_plan("goal")
    assert plan.tasks[0].agent == AgentName.GENERAL


async def test_planner_falls_back_on_invalid_json():
    planner = Planner(ScriptedLLM("I cannot produce JSON, sorry!"))
    plan, _ = await planner.create_plan("Please analyze the sales data numbers")
    assert len(plan.tasks) == 1  # fallback single-task plan
    assert "fell back" in plan.analysis.lower()
    # Fallback routing should still pick a sensible agent for the goal.
    assert plan.tasks[0].agent == AgentName.NUMBER


async def test_planner_filters_disallowed_agents():
    raw = _plan_json([
        {"id": "t1", "agent": "web_search", "description": "search", "depends_on": [], "reason": "r"},
        {"id": "t2", "agent": "general", "description": "answer", "depends_on": [], "reason": "r"},
    ])
    planner = Planner(ScriptedLLM(raw))
    plan, _ = await planner.create_plan("goal", allowed_agents=[AgentName.GENERAL])
    assert {t.agent for t in plan.tasks} == {AgentName.GENERAL}


def test_task_plan_waves_detect_cycle():
    from backend.schemas import TaskPlan, TaskSpec

    plan = TaskPlan(tasks=[
        TaskSpec(id="t1", agent=AgentName.GENERAL, description="a", depends_on=["t2"]),
        TaskSpec(id="t2", agent=AgentName.GENERAL, description="b", depends_on=["t1"]),
    ])
    try:
        plan.waves()
        raise AssertionError("cycle should raise")
    except ValueError as exc:
        assert "Cyclic" in str(exc) or "dangling" in str(exc)
