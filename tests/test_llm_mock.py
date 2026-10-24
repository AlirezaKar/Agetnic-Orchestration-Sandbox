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


# ──────────────────────────────────────────────────────────────────
#  GroqClient HTTP contract & retired-model migration
# ──────────────────────────────────────────────────────────────────
async def test_groq_client_migrates_deprecated_model_on_404(monkeypatch):
    import httpx
    from backend.config import Settings
    from backend.llm.groq_client import GroqClient

    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        if body["model"] == "llama-3.3-70b-versatile":
            return httpx.Response(
                404,
                json={
                    "error": {
                        "message": "The model `llama-3.3-70b-versatile` does not exist or you do not have access to it.",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": "<think>internal</think>Konnichiwa"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("backend.llm.groq_client.httpx.AsyncClient", fake_client)

    client = GroqClient(
        Settings(groq_api_key="gsk_test", groq_model_orchestrator="llama-3.3-70b-versatile")
    )
    res = await client.complete([LLMMessage("user", "hello in japanese")])

    assert [p["model"] for p in seen_payloads] == [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
    ]
    assert "max_completion_tokens" in seen_payloads[1]
    assert res.model == "openai/gpt-oss-120b"
    assert res.content == "Konnichiwa"


async def test_groq_client_unknown_model_404_gives_actionable_error(monkeypatch):
    import httpx
    from backend.config import Settings
    from backend.llm.base import LLMError
    from backend.llm.groq_client import GroqClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"message": "not found", "code": "model_not_found"}},
        )

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("backend.llm.groq_client.httpx.AsyncClient", fake_client)

    client = GroqClient(Settings(groq_api_key="gsk_test", groq_model_orchestrator="typo-model"))
    with pytest.raises(LLMError, match="GROQ_MODEL_ORCHESTRATOR"):
        await client.complete([LLMMessage("user", "hi")])
