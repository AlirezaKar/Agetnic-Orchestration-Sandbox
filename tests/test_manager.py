"""
tests.test_manager — the Manager-rooted orchestration flow (auto + manual).
"""

import json

from backend.agents.base import AgentContext, BaseAgent
from backend.config import settings
from backend.llm.mock_client import MockLLMClient
from backend.orchestration.manager import Manager
from backend.schemas import AgentName, AgentResult, ChatRequest, Mode
from backend.orchestration.trace import load_trace


class FlakyAgent(BaseAgent):
    """Fails on its first call, then succeeds — exercises re-planning."""

    name = AgentName.GENERAL

    def __init__(self):
        super().__init__(MockLLMClient())
        self.calls = 0

    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure")
        return AgentResult(agent=self.name, success=True, output="recovered ok")


async def test_auto_mode_full_flow(manager):
    """Auto mode: analyse → plan → parallel agents → synthesis, all traced."""
    request = ChatRequest(
        message="Search the web for orchestration trends and analyze the numbers",
        mode=Mode.AUTO,
    )
    response = await manager.run(request)

    assert response.success
    assert response.mode_used == Mode.AUTO
    assert response.output
    assert response.thinking  # Manager analysis recorded
    assert response.plan is not None
    assert len(response.plan.tasks) >= 2

    # Process recorded: plan step + agent tasks + synthesis.
    step_types = [s.type for s in response.steps]
    assert "plan" in step_types
    assert "agent_task" in step_types
    assert "synthesis" in step_types

    # The persisted trace contains input/thinking/process/output.
    trace = load_trace(response.run_id)
    assert trace is not None
    assert trace.input == request.message
    assert trace.thinking
    assert trace.steps
    assert trace.output == response.output


async def test_auto_mode_simple_prompt_uses_general(manager):
    response = await manager.run(ChatRequest(message="please explain what a variable is", mode=Mode.AUTO))
    assert response.success
    agents = response.metrics["agents_used"]
    assert "general" in agents


async def test_manual_mode_delegates_to_pinned_agent(manager):
    response = await manager.run(
        ChatRequest(message="Compute 2+2*5 quickly", mode=Mode.NUMBER)
    )
    assert response.mode_used == Mode.NUMBER
    assert response.metrics["agents_used"] == ["number"]
    # Number agent's local tool computes this exactly.
    assert "2+2*5" in response.output or "12" in response.output


async def test_replan_on_failed_task(manager, monkeypatch):
    """A failing task must trigger exactly one re-plan attempt."""
    flaky = FlakyAgent()
    manager._agents = {
        AgentName.DOCUMENT: flaky,
        AgentName.WEB_SEARCH: flaky,
        AgentName.GENERAL: flaky,
        AgentName.NUMBER: flaky,
    }
    request = ChatRequest(message="please explain what a variable is", mode=Mode.AUTO)
    response = await manager.run(request)

    assert response.success
    assert response.metrics["replan_count"] == 1
    assert flaky.calls == 2  # failed once, then recovered
    assert response.metrics["tasks_failed"] == 0  # after the successful re-plan


async def test_metrics_and_trace_file_field(manager):
    response = await manager.run(ChatRequest(message="hello!", mode=Mode.GENERAL))
    assert response.metrics["tasks_total"] >= 1
    assert "total_duration_ms" in response.metrics
    assert response.trace_file.endswith(f"{response.run_id}.json")
