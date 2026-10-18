"""
tests.test_router — hand-off routing (manual modes + fallback).
"""

from backend.orchestration.router import Router
from backend.schemas import AgentName, Mode


def test_manual_mode_mapping():
    router = Router()
    assert router.agent_for_mode(Mode.DOCUMENT) == AgentName.DOCUMENT
    assert router.agent_for_mode(Mode.WEB_SEARCH) == AgentName.WEB_SEARCH
    assert router.agent_for_mode(Mode.GENERAL) == AgentName.GENERAL
    assert router.agent_for_mode(Mode.NUMBER) == AgentName.NUMBER
    assert router.agent_for_mode(Mode.AUTO) is None


def test_keyword_fallback_routing():
    router = Router()
    assert router.choose_agent("please search the web for the latest news") == AgentName.WEB_SEARCH
    assert router.choose_agent("analyze the sales data and statistics") == AgentName.NUMBER
    assert router.choose_agent("according to the document, what is the policy?") == AgentName.DOCUMENT
    assert router.choose_agent("write a poem about the sea") == AgentName.GENERAL


def test_explain_is_human_readable():
    text = Router().explain("search for something")
    assert "web_search" in text
