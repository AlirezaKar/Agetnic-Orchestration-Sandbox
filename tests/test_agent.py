"""Agent loop tests: tool chaining, error feedback, step limits, tracing."""

from __future__ import annotations

import unittest

from app.agent import WebSearchAgent
from app.config import Settings
from app.llm.base import ModelResponse, ProviderError, ToolCall
from app.search.base import SearchError, SearchResponse, SearchResult


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class ScriptedChain:
    """Stands in for ModelChain: replays canned responses, records the requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools, **kwargs})
        if not self.responses:
            return ModelResponse(content="fallback", provider="fake", model="fake-1")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSearch:
    def __init__(self, results=None, error=None, provider="ddg"):
        self.results = results or []
        self.error = error
        self.provider = provider
        self.queries = []

    async def search(self, query, max_results=None):
        self.queries.append((query, max_results))
        if self.error:
            raise self.error
        return SearchResponse(
            query=query,
            results=self.results,
            provider="ddg",
            attempts=[{"provider": "ddg", "ok": True}],
            latency_ms=5,
        )


def search_response_payload():
    return [
        SearchResult(
            title="Python 3.13 release notes",
            url="https://docs.python.org/3/whatsnew/3.13.html",
            snippet="Python 3.13 is the newest major release.",
            source="ddg",
        )
    ]


class AgentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_then_answer(self):
        tool_call = ToolCall(name="web_search", arguments={"query": "python 3.13 release"}, id="c1")
        chain = ScriptedChain(
            [
                ModelResponse(content="", tool_calls=[tool_call], provider="groq", model="llama"),
                ModelResponse(content="Python 3.13 is current [1].", provider="groq", model="llama"),
            ]
        )
        search = FakeSearch(search_response_payload())
        agent = WebSearchAgent(chain, search, settings(agent_max_steps=4))

        result = await agent.run("What is the newest Python?")

        self.assertEqual(result.answer, "Python 3.13 is current [1].")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.search_provider, "ddg")
        self.assertEqual(result.sources, ["https://docs.python.org/3/whatsnew/3.13.html"])
        self.assertEqual(search.queries[0][0], "python 3.13 release")
        self.assertEqual([t["type"] for t in result.trace], ["model", "tool", "model"])
        self.assertTrue(result.trace[1]["ok"])
        # second model call must contain the assistant tool_call + the tool result
        roles = [m["role"] for m in chain.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertIn("Python 3.13", chain.requests[1]["messages"][-1]["content"])

    async def test_tools_are_offered_to_the_model(self):
        chain = ScriptedChain([ModelResponse(content="hi", provider="p", model="m")])
        agent = WebSearchAgent(chain, FakeSearch(), settings())
        await agent.run("hello")
        tools = chain.requests[0]["tools"]
        self.assertEqual({t["function"]["name"] for t in tools}, {"web_search", "fetch_url"})

    async def test_allow_search_false_hides_tools(self):
        chain = ScriptedChain([ModelResponse(content="hi", provider="p", model="m")])
        agent = WebSearchAgent(chain, FakeSearch(), settings())
        await agent.run("hello", allow_search=False)
        self.assertIsNone(chain.requests[0]["tools"])

    async def test_search_failure_is_reported_back_to_the_model(self):
        tool_call = ToolCall(name="web_search", arguments={"query": "x"}, id="c1")
        chain = ScriptedChain(
            [
                ModelResponse(content="", tool_calls=[tool_call], provider="p", model="m"),
                ModelResponse(content="I could not search.", provider="p", model="m"),
            ]
        )
        agent = WebSearchAgent(chain, FakeSearch(error=SearchError("ddg", "bot challenge")), settings())

        result = await agent.run("q")

        self.assertEqual(result.answer, "I could not search.")
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["tool"], "web_search")
        self.assertFalse(result.trace[1]["ok"])
        self.assertIn("error", chain.requests[1]["messages"][-1]["content"])

    async def test_step_limit_forces_a_final_answer_without_tools(self):
        call = ToolCall(name="web_search", arguments={"query": "loop"}, id="c")
        chain = ScriptedChain(
            [
                ModelResponse(content="", tool_calls=[call], provider="p", model="m"),
                ModelResponse(content="", tool_calls=[call], provider="p", model="m"),
                ModelResponse(content="final summary", provider="p", model="m"),
            ]
        )
        agent = WebSearchAgent(chain, FakeSearch(search_response_payload()), settings(agent_max_steps=2))
        result = await agent.run("q")
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(result.answer, "final summary")
        self.assertIsNone(chain.requests[-1]["tools"])

    async def test_unknown_tool_is_handled(self):
        call = ToolCall(name="rm_rf", arguments={}, id="c")
        chain = ScriptedChain(
            [
                ModelResponse(content="", tool_calls=[call], provider="p", model="m"),
                ModelResponse(content="ok", provider="p", model="m"),
            ]
        )
        agent = WebSearchAgent(chain, FakeSearch(), settings())
        result = await agent.run("q")
        self.assertFalse(result.trace[1]["ok"])
        self.assertIn("unknown tool", str(result.errors))

    async def test_max_results_is_clamped(self):
        call = ToolCall(name="web_search", arguments={"query": "x", "max_results": 99}, id="c")
        chain = ScriptedChain(
            [
                ModelResponse(content="", tool_calls=[call], provider="p", model="m"),
                ModelResponse(content="ok", provider="p", model="m"),
            ]
        )
        search = FakeSearch(search_response_payload())
        agent = WebSearchAgent(chain, search, settings())
        await agent.run("q")
        self.assertEqual(search.queries[0][1], 10)

    async def test_empty_query_is_rejected_before_hitting_the_network(self):
        call = ToolCall(name="web_search", arguments={"query": "   "}, id="c")
        chain = ScriptedChain(
            [
                ModelResponse(content="", tool_calls=[call], provider="p", model="m"),
                ModelResponse(content="ok", provider="p", model="m"),
            ]
        )
        search = FakeSearch()
        agent = WebSearchAgent(chain, search, settings())
        result = await agent.run("q")
        self.assertEqual(search.queries, [])
        self.assertFalse(result.trace[1]["ok"])

    async def test_provider_error_is_raised_for_the_http_layer(self):
        chain = ScriptedChain([ProviderError("groq", "HTTP 401", status=401)])
        agent = WebSearchAgent(chain, FakeSearch(), settings())
        with self.assertRaises(ProviderError):
            await agent.run("q")

    async def test_history_is_forwarded_and_truncated(self):
        chain = ScriptedChain([ModelResponse(content="ok", provider="p", model="m")])
        agent = WebSearchAgent(chain, FakeSearch(), settings())
        await agent.run("now", history=[{"role": "user", "content": "x" * 20000}])
        messages = chain.requests[0]["messages"]
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(len(messages[1]["content"]), 8000)


if __name__ == "__main__":
    unittest.main()
