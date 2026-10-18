"""
tests.test_llm_mock — deterministic offline LLM behaviour.
"""

import json

import pytest

from backend.llm import get_agent_llm, get_orchestrator_llm
from backend.llm.mock_client import MockLLMClient
from backend.llm.base import LLMMessage


@pytest.fixture
def mock_llm():
    return MockLLMClient()


async def test_planner_role_returns_valid_plan_json(mock_llm):
    response = await mock_llm.complete(
        [
            LLMMessage("system", "You are the Planner of a multi-agent orchestration system."),
            LLMMessage("user", "## USER GOAL\nSearch the web for market data and analyze it"),
        ],
        json_mode=True,
    )
    plan = json.loads(response.content)
    assert "tasks" in plan
    agents = {t["agent"] for t in plan["tasks"]}
    assert "web_search" in agents
    assert "number" in agents


async def test_manager_role_analysis(mock_llm):
    response = await mock_llm.complete(
        [
            LLMMessage("system", "You are the Manager of a multi-agent orchestration system."),
            LLMMessage("user", "## USER GOAL\nhello there"),
        ]
    )
    assert "Manager" in response.content
    assert response.mocked is True


async def test_generic_role_json_mode(mock_llm):
    response = await mock_llm.complete(
        [LLMMessage("system", "You are a helpful assistant."), LLMMessage("user", "hi")],
        json_mode=True,
    )
    data = json.loads(response.content)  # must still be valid JSON
    assert "response" in data


async def test_factory_returns_mock_without_key():
    assert get_orchestrator_llm().provider == "mock"
    assert get_agent_llm().provider == "mock"


async def test_usage_accounting(mock_llm):
    response = await mock_llm.complete(
        [LLMMessage("system", "You are the General Agent."), LLMMessage("user", "hello")]
    )
    assert response.usage["total_tokens"] > 0
