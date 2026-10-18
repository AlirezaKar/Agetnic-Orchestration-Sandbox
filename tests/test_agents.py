"""
tests.test_agents — worker-agent behaviour (incl. the safe math tool).
"""

from backend.agents.base import AgentContext
from backend.agents.number_agent import extract_expression, safe_calculate
from backend.agents.registry import get_agent
from backend.agents.web_search_agent import _query_from_task
from backend.agents.document_agent import _question_from_task
from backend.schemas import AgentName


# ──────────────────────────────────────────────────────────────────
#  Number agent: safe arithmetic tool
# ──────────────────────────────────────────────────────────────────
def test_safe_calculate_basic():
    assert safe_calculate("2+2*5") == 12
    assert safe_calculate("12.5*(3+1)/2") == 25.0
    assert safe_calculate("(1+2)**3") == 27


def test_safe_calculate_rejects_code_injection():
    assert safe_calculate("__import__('os').system('ls')") is None
    assert safe_calculate("open('/etc/passwd')") is None
    assert safe_calculate("a+b") is None


def test_safe_calculate_rejects_huge_exponents():
    # Would consume unbounded memory without the guard.
    assert safe_calculate("2**999999999999") is None


def test_extract_expression_from_text():
    expr = extract_expression("What is 12.5*(3+1)/2 equal to?")
    assert expr is not None
    assert safe_calculate(expr) == 25.0
    assert extract_expression("no numbers here") is None


async def test_number_agent_uses_tool_and_llm():
    agent = get_agent(AgentName.NUMBER)
    result = await agent.run("Calculate 12.5*(3+1)/2 and explain the result", AgentContext())
    assert result.success
    assert "25.0" in result.output
    assert result.data["computed_expression"] is not None
    assert result.thinking


# ──────────────────────────────────────────────────────────────────
#  Web search agent
# ──────────────────────────────────────────────────────────────────
def test_query_extraction():
    assert _query_from_task('Search the web for up-to-date information relevant to: "llm trends"') == "llm trends"
    assert _query_from_task("search for the latest AI news") == "the latest AI news"


async def test_web_search_agent_simulated_results():
    agent = get_agent(AgentName.WEB_SEARCH)
    result = await agent.run(
        'Search the web for up-to-date information relevant to: "multi-agent orchestration"',
        AgentContext(),
    )
    assert result.success
    assert result.data["search"]["simulated"] is True
    assert len(result.data["search"]["results"]) >= 3
    assert "multi-agent orchestration" in result.data["query"]


# ──────────────────────────────────────────────────────────────────
#  Document agent (RAG)
# ──────────────────────────────────────────────────────────────────
def test_question_extraction():
    assert _question_from_task(
        'Retrieve and use knowledge from uploaded documents relevant to: "vacation policy?"'
    ) == "vacation policy?"


async def test_document_agent_uses_store(document_store):
    document_store.add_document(
        "policy.txt",
        "The refund policy allows returns within 30 days of purchase with a receipt.",
    )
    agent = get_agent(AgentName.DOCUMENT)
    result = await agent.run(
        'Retrieve and use knowledge from uploaded documents relevant to: "what is the refund policy?"',
        AgentContext(),
    )
    assert result.success
    assert result.data["retrieved_chunks"], "RAG should retrieve at least one chunk"
    assert any("refund" in c["text"].lower() for c in result.data["retrieved_chunks"])


# ──────────────────────────────────────────────────────────────────
#  General agent
# ──────────────────────────────────────────────────────────────────
async def test_general_agent_answers_with_history():
    agent = get_agent(AgentName.GENERAL)
    ctx = AgentContext(history=[{"role": "user", "content": "my name is Sam"}])
    result = await agent.run("What is my name?", ctx)
    assert result.success
    assert result.output
