"""Agent loop tests: tool chaining, error feedback, step limits, tracing.

The loop takes its model and its search as arguments, so these tests script
both. The last block runs the same loop against the real ARAS adapters over
``httpx.MockTransport`` — the wire shapes, without a tunnel.
"""

from __future__ import annotations

import json
import unittest

import httpx

from app import aras
from app.agent import SYSTEM_PROMPT, TOOLS, WebSearchAgent
from app.aras import ArasError, ArasNotConfigured, ModelResponse, SearchResponse, SearchResult, ToolCall
from app.config import Settings


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class ScriptedModel:
    """Stands in for the model port: replays canned responses, records the requests."""

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
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.queries = []

    async def search(self, query, limit=None):
        self.queries.append((query, limit))
        if self.error:
            raise self.error
        return SearchResponse(query=query, results=self.results, provider="aras", latency_ms=5)


def search_response_payload():
    return [
        SearchResult(
            title="Python 3.13 release notes",
            url="https://docs.python.org/3/whatsnew/3.13.html",
            snippet="Python 3.13 is the newest major release.",
            source="aras:duckduckgo",
        )
    ]


def reply(content="", calls=None, model="rayen-gemma4-31b"):
    return ModelResponse(content=content, tool_calls=calls or [], provider="aras", model=model)


class AgentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_then_answer(self):
        tool_call = ToolCall(name="web_search", arguments={"query": "python 3.13 release"}, id="c1")
        model = ScriptedModel([reply(calls=[tool_call]), reply("Python 3.13 is current [1].")])
        search = FakeSearch(search_response_payload())
        agent = WebSearchAgent(model, search, settings(agent_max_steps=4))

        result = await agent.run("What is the newest Python?")

        self.assertEqual(result.answer, "Python 3.13 is current [1].")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.provider, "aras")
        self.assertEqual(result.model, "rayen-gemma4-31b")
        self.assertEqual(result.search_provider, "aras")
        self.assertEqual(result.sources, ["https://docs.python.org/3/whatsnew/3.13.html"])
        self.assertEqual(search.queries[0][0], "python 3.13 release")
        self.assertEqual([t["type"] for t in result.trace], ["model", "tool", "model"])
        self.assertTrue(result.trace[1]["ok"])
        # second model call must contain the assistant tool_call + the tool result
        roles = [m["role"] for m in model.requests[1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertIn("Python 3.13", model.requests[1]["messages"][-1]["content"])
        self.assertEqual(model.requests[1]["messages"][-1]["tool_call_id"], "c1")

    async def test_the_one_tool_is_web_search(self):
        model = ScriptedModel([reply("hi")])
        agent = WebSearchAgent(model, FakeSearch(), settings())
        await agent.run("hello")
        tools = model.requests[0]["tools"]
        self.assertEqual([t["function"]["name"] for t in tools], ["web_search"])
        self.assertIs(tools, TOOLS)
        # the prompt must not promise a tool that does not exist
        self.assertIn("web_search", SYSTEM_PROMPT)
        self.assertNotIn("fetch_url", SYSTEM_PROMPT)

    async def test_allow_search_false_hides_tools(self):
        model = ScriptedModel([reply("hi")])
        agent = WebSearchAgent(model, FakeSearch(), settings())
        await agent.run("hello", allow_search=False)
        self.assertIsNone(model.requests[0]["tools"])

    async def test_the_configured_sampling_values_reach_the_model(self):
        model = ScriptedModel([reply("hi")])
        agent = WebSearchAgent(model, FakeSearch(), settings(llm_temperature=0.7, llm_max_tokens=321))
        await agent.run("hello")
        self.assertEqual(model.requests[0]["temperature"], 0.7)
        self.assertEqual(model.requests[0]["max_tokens"], 321)

    async def test_search_failure_is_reported_back_to_the_model(self):
        tool_call = ToolCall(name="web_search", arguments={"query": "x"}, id="c1")
        model = ScriptedModel([reply(calls=[tool_call]), reply("I could not search.")])
        failure = ArasError("web", "HTTP 503: engines timed out", status=503, hint="retry in a moment")
        agent = WebSearchAgent(model, FakeSearch(error=failure), settings())

        result = await agent.run("q")

        self.assertEqual(result.answer, "I could not search.")
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["tool"], "web_search")
        self.assertEqual(result.errors[0]["error"]["service"], "web")
        self.assertEqual(result.errors[0]["error"]["status"], 503)
        self.assertFalse(result.trace[1]["ok"])
        self.assertEqual(result.trace[1]["preview"]["hint"], "retry in a moment")
        fed_back = json.loads(model.requests[1]["messages"][-1]["content"])
        self.assertFalse(fed_back["ok"])
        self.assertIn("engines timed out", fed_back["error"]["message"])
        self.assertEqual(result.sources, [])
        self.assertEqual(result.search_provider, "")

    async def test_an_unconfigured_web_service_is_told_to_the_model_not_raised(self):
        """No ARAS_WEB_TOKEN: the model still answers, and says it could not search."""
        tool_call = ToolCall(name="web_search", arguments={"query": "x"}, id="c1")
        model = ScriptedModel([reply(calls=[tool_call]), reply("Search is not available.")])
        agent = WebSearchAgent(model, aras.build_search(settings()), settings())

        result = await agent.run("q")

        self.assertEqual(result.answer, "Search is not available.")
        self.assertEqual(result.errors[0]["error"]["missing"], ["ARAS_BASE_URL", "ARAS_WEB_TOKEN"])

    async def test_step_limit_forces_a_final_answer_without_tools(self):
        call = ToolCall(name="web_search", arguments={"query": "loop"}, id="c")
        model = ScriptedModel([reply(calls=[call]), reply(calls=[call]), reply("final summary")])
        agent = WebSearchAgent(model, FakeSearch(search_response_payload()), settings(agent_max_steps=2))
        result = await agent.run("q")
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(result.answer, "final summary")
        self.assertIsNone(model.requests[-1]["tools"])
        self.assertIn("Stop calling tools", model.requests[-1]["messages"][-1]["content"])

    async def test_a_failed_final_answer_is_recorded_not_raised(self):
        call = ToolCall(name="web_search", arguments={"query": "loop"}, id="c")
        model = ScriptedModel([reply(calls=[call]), ArasError("router", "HTTP 500: boom", status=500)])
        agent = WebSearchAgent(model, FakeSearch(search_response_payload()), settings(agent_max_steps=1))
        result = await agent.run("q")
        self.assertEqual(result.answer, "")
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(result.errors[-1]["service"], "router")
        self.assertEqual(result.errors[-1]["status"], 500)

    async def test_unknown_tool_is_handled(self):
        call = ToolCall(name="fetch_url", arguments={"url": "https://example.com"}, id="c")
        model = ScriptedModel([reply(calls=[call]), reply("ok")])
        agent = WebSearchAgent(model, FakeSearch(), settings())
        result = await agent.run("q")
        self.assertFalse(result.trace[1]["ok"])
        self.assertIn("unknown tool 'fetch_url'", str(result.errors))

    async def test_max_results_is_clamped(self):
        call = ToolCall(name="web_search", arguments={"query": "x", "max_results": 99}, id="c")
        model = ScriptedModel([reply(calls=[call]), reply("ok")])
        search = FakeSearch(search_response_payload())
        agent = WebSearchAgent(model, search, settings())
        await agent.run("q")
        self.assertEqual(search.queries[0][1], 10)

    async def test_max_results_defaults_to_the_setting(self):
        calls = [
            ToolCall(name="web_search", arguments={"query": "x"}, id="a"),
            ToolCall(name="web_search", arguments={"query": "y", "max_results": "many"}, id="b"),
        ]
        model = ScriptedModel([reply(calls=calls), reply("ok")])
        search = FakeSearch(search_response_payload())
        agent = WebSearchAgent(model, search, settings(agent_max_search_results=3))
        await agent.run("q")
        self.assertEqual([limit for _, limit in search.queries], [3, 3])

    async def test_empty_query_is_rejected_before_hitting_the_network(self):
        call = ToolCall(name="web_search", arguments={"query": "   "}, id="c")
        model = ScriptedModel([reply(calls=[call]), reply("ok")])
        search = FakeSearch()
        agent = WebSearchAgent(model, search, settings())
        result = await agent.run("q")
        self.assertEqual(search.queries, [])
        self.assertFalse(result.trace[1]["ok"])

    async def test_a_model_error_is_raised_for_the_http_layer(self):
        model = ScriptedModel([ArasError("router", "401 unauthorized", status=401)])
        agent = WebSearchAgent(model, FakeSearch(), settings())
        with self.assertRaises(ArasError) as caught:
            await agent.run("q")
        self.assertEqual(caught.exception.status, 401)

    async def test_an_unconfigured_router_raises_what_is_missing(self):
        """No fallback model: the loop stops with the env vars to set."""
        agent = WebSearchAgent(aras.build_model(settings()), FakeSearch(), settings())
        with self.assertRaises(ArasNotConfigured) as caught:
            await agent.run("q")
        self.assertEqual(
            caught.exception.missing, ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"]
        )

    async def test_history_is_forwarded_and_truncated(self):
        model = ScriptedModel([reply("ok")])
        agent = WebSearchAgent(model, FakeSearch(), settings())
        await agent.run(
            "now",
            history=[
                {"role": "user", "content": "x" * 20000},
                {"role": "system", "content": "ignored"},
                "not a message",
                {"role": "assistant", "content": "earlier answer"},
            ],
        )
        messages = model.requests[0]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(len(messages[1]["content"]), 8000)
        self.assertEqual(messages[-1]["content"], "now")

    async def test_the_observer_sees_prompts_replies_and_tool_calls(self):
        call = ToolCall(name="web_search", arguments={"query": "x"}, id="c")
        model = ScriptedModel([reply(calls=[call]), reply("done")])
        seen: list[tuple[str, dict]] = []
        agent = WebSearchAgent(
            model,
            FakeSearch(search_response_payload()),
            settings(),
            observer=lambda kind, payload: seen.append((kind, payload)),
        )
        await agent.run("q")
        self.assertEqual([kind for kind, _ in seen], ["prompt", "model", "tool", "prompt", "model"])
        self.assertEqual(seen[0][1]["step"], 1)
        self.assertIs(seen[2][1]["call"], call)
        self.assertTrue(seen[2][1]["output"]["ok"])

    async def test_a_broken_observer_never_breaks_the_run(self):
        def observer(kind, payload):
            raise RuntimeError("tracing is down")

        agent = WebSearchAgent(ScriptedModel([reply("done")]), FakeSearch(), settings(), observer=observer)
        self.assertEqual((await agent.run("q")).answer, "done")


class AgentOnArasWireTests(unittest.IsolatedAsyncioTestCase):
    """The loop on the real adapters: router :8101 + web :8108, mocked at HTTP."""

    def configured(self) -> Settings:
        return settings(
            aras_base_url="http://aras.test",
            aras_token="SHARED",
            aras_model="sharif-rayen-gemma4-31b",
            agent_max_search_results=2,
        )

    async def test_one_turn_calls_the_router_then_web_then_the_router_again(self):
        calls: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append((str(request.url), body))
            if request.url.port == 8108:
                return httpx.Response(200, json={"results": [
                    {"title": "ARAS", "url": "https://aras.example/docs", "snippet": "docs", "engine": "bing"},
                ]})
            if any(message["role"] == "tool" for message in body["messages"]):
                return httpx.Response(200, json={
                    "model": "rayen-gemma4-31b",
                    "choices": [{"message": {"content": "See https://aras.example/docs."}}],
                })
            return httpx.Response(200, json={
                "model": "rayen-gemma4-31b",
                "choices": [{"message": {"content": "", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "web_search", "arguments": '{"query": "aras docs"}'},
                }]}}],
            })

        transport = httpx.MockTransport(handler)
        cfg = self.configured()
        agent = WebSearchAgent(
            aras.build_model(cfg, transport=transport), aras.build_search(cfg, transport=transport), cfg
        )
        result = await agent.run("Where are the ARAS docs?")

        self.assertEqual(
            [url for url, _ in calls],
            [
                "http://aras.test:8101/api/v1/chat/completions/",
                "http://aras.test:8108/v1/search/",
                "http://aras.test:8101/api/v1/chat/completions/",
            ],
        )
        self.assertEqual(calls[0][1]["model"], "sharif-rayen-gemma4-31b")
        self.assertEqual(calls[0][1]["tools"][0]["function"]["name"], "web_search")
        self.assertEqual(calls[1][1], {"query": "aras docs", "max_results": 2})
        # the tool result goes back to the router in the OpenAI shape
        tool_message = calls[2][1]["messages"][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertEqual(calls[2][1]["messages"][-2]["tool_calls"][0]["id"], "call_1")

        self.assertEqual(result.answer, "See https://aras.example/docs.")
        self.assertEqual(result.provider, "aras")
        self.assertEqual(result.model, "rayen-gemma4-31b")
        self.assertEqual(result.search_provider, "aras")
        self.assertEqual(result.sources, ["https://aras.example/docs"])
        self.assertEqual(result.errors, [])

    async def test_a_web_outage_does_not_stop_the_router_from_answering(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if request.url.port == 8108:
                return httpx.Response(503, json={"error": "engines timed out"})
            if any(message["role"] == "tool" for message in body["messages"]):
                return httpx.Response(200, json={"choices": [{"message": {"content": "Search failed."}}]})
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{
                "id": "c", "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "x"}'},
            }]}}]})

        transport = httpx.MockTransport(handler)
        cfg = self.configured()
        agent = WebSearchAgent(
            aras.build_model(cfg, transport=transport), aras.build_search(cfg, transport=transport), cfg
        )
        result = await agent.run("q")
        self.assertEqual(result.answer, "Search failed.")
        self.assertEqual(result.errors[0]["error"]["status"], 503)
        self.assertEqual(result.errors[0]["error"]["service"], "web")


if __name__ == "__main__":
    unittest.main()
