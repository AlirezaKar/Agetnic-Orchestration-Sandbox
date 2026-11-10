"""The ARAS wiring: config, the model port, the search port, the clients.

Everything here runs against ``httpx.MockTransport`` — no tunnel, no token,
no network. What is being proven, per block:

* config     — URL derivation, token fallback, and ``aras_missing``: the one
               place that says which env vars a service still needs
* model      — the router is OpenAI-compatible *with a trailing slash*; a
               POST to ``.../chat/completions`` (no slash) would die on the
               301 redirect, so the exact URL is asserted. No fallback: an
               unconfigured router is an error that names what is missing
* search     — the web service is the only backend; a 503 is an error the
               caller sees, and an unconfigured one names its env vars
* catalogue  — what GET /providers shows for each service
* clients    — multipart OCR, retrieval bodies, the data service's tenant
               headers, 401 hints
* ports      — the document agent retrieves when there is no attachment and
               a knowledge store is wired in; the data agent queries the
               backend when there is nothing local to compute; and both
               keep their old behaviour when the ports are None
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

import httpx

from app import aras
from app.config import Settings
from app.orchestrator.agents import AgentContext, run_data, run_document
from app.orchestrator.files import extract
from app.orchestrator.planner import PlanStep

from tests.test_orchestrator import FakeModel


def aras_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        _env_file=None,
        aras_base_url="http://aras.test",
        aras_token="SHARED",
        aras_tenant="alireza-kalaie",
        aras_owner="alireza-kalaie",
        aras_collection="alireza_notes",
        aras_data_source_id="ds-1",
        aras_model="sharif-rayen-gemma4-31b",
        orchestrator_log_runs=False,
    )
    base.update(overrides)
    return Settings(**base)


def run(coro):
    return asyncio.run(coro)


def json_transport(payload: Any, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


CHAT_OK = {
    "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
    "model": "rayen-gemma4-31b",
    "usage": {"total_tokens": 7},
}


# ----------------------------------------------------------------- config
class ArasConfigTests(unittest.TestCase):
    def test_endpoints_derive_from_the_base_url_and_known_ports(self) -> None:
        s = aras_settings()
        self.assertEqual(s.aras_endpoint("router"), "http://aras.test:8101/api/v1")
        self.assertEqual(s.aras_endpoint("ocr"), "http://aras.test:8102")
        self.assertEqual(s.aras_endpoint("data"), "http://aras.test:8107")
        self.assertEqual(s.aras_endpoint("web"), "http://aras.test:8108")

    def test_a_full_url_override_wins_over_the_derived_one(self) -> None:
        s = aras_settings(aras_web_url="http://elsewhere:9999/")
        self.assertEqual(s.aras_endpoint("web"), "http://elsewhere:9999")

    def test_every_service_token_falls_back_to_the_shared_token(self) -> None:
        s = aras_settings(aras_web_token="OWN")
        self.assertEqual(s.aras_web_token, "OWN")
        for name in ("router", "ocr", "vector", "ingestion", "retrieval", "data"):
            self.assertEqual(getattr(s, f"aras_{name}_token"), "SHARED", name)

    def test_nothing_is_configured_until_a_url_and_token_exist(self) -> None:
        s = Settings(_env_file=None)
        self.assertEqual(s.aras_services_configured, [])
        self.assertFalse(s.aras_model_ready)
        self.assertFalse(s.aras_search_ready)
        self.assertFalse(s.aras_rag_ready)
        self.assertFalse(s.aras_data_ready)

    def test_a_full_block_makes_every_port_ready(self) -> None:
        s = aras_settings()
        self.assertTrue(s.aras_model_ready)
        self.assertTrue(s.aras_search_ready)
        for name in aras.CLIENTS:
            self.assertEqual(s.aras_missing(name), [], name)

    def test_missing_names_exactly_the_env_vars_to_set(self) -> None:
        empty = Settings(_env_file=None)
        self.assertEqual(
            empty.aras_missing("router"), ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"]
        )
        self.assertEqual(empty.aras_missing("web"), ["ARAS_BASE_URL", "ARAS_WEB_TOKEN"])
        # a URL but no token: only the token is named
        self.assertEqual(
            Settings(_env_file=None, aras_base_url="http://aras.test").aras_missing("web"),
            ["ARAS_WEB_TOKEN"],
        )
        # a per-service URL stands in for the base URL
        self.assertEqual(
            Settings(
                _env_file=None, aras_web_url="http://w.test:8108", aras_web_token="T"
            ).aras_missing("web"),
            [],
        )
        self.assertEqual(aras_settings(aras_model=None).aras_missing("router"), ["ARAS_MODEL"])
        self.assertEqual(
            aras_settings(aras_collection="").aras_missing("retrieval"), ["ARAS_COLLECTION"]
        )
        self.assertEqual(
            aras_settings(aras_tenant=None, aras_data_source_id=None).aras_missing("data"),
            ["ARAS_TENANT", "ARAS_DATA_SOURCE_ID"],
        )

    def test_auto_is_not_a_model_id(self) -> None:
        """Nothing picks a model on the user's behalf: ``auto`` counts as unset."""
        for value in ("auto", "AUTO", " auto ", "", "   "):
            s = aras_settings(aras_model=value)
            self.assertEqual(s.aras_model_id, "", repr(value))
            self.assertIn("ARAS_MODEL", s.aras_missing("router"), repr(value))
            self.assertFalse(s.aras_model_ready, repr(value))
        self.assertEqual(aras_settings(aras_model=" m-1 ").aras_model_id, "m-1")

    def test_readiness_needs_every_field_it_claims_to_need(self) -> None:
        self.assertTrue(aras_settings().aras_rag_ready)
        self.assertTrue(aras_settings().aras_data_ready)
        self.assertFalse(aras_settings(aras_collection="").aras_rag_ready)
        self.assertFalse(aras_settings(aras_data_source_id=None).aras_data_ready)
        self.assertFalse(aras_settings(aras_tenant=None).aras_data_ready)

    def test_the_public_summary_names_services_but_never_tokens(self) -> None:
        summary = aras_settings().public_summary()
        self.assertIn("router", summary["aras_services"])
        self.assertEqual(summary["model_provider"], "aras")
        self.assertEqual(summary["search_provider"], "aras")
        self.assertEqual(summary["model"], "sharif-rayen-gemma4-31b")
        self.assertTrue(summary["model_ready"] and summary["search_ready"])
        self.assertNotIn("SHARED", json.dumps(summary))

    def test_settings_know_no_other_provider(self) -> None:
        """The old provider/search settings are gone, not merely unused."""
        fields = set(Settings.model_fields)
        for gone in (
            "llm_provider", "llm_model", "llm_fallbacks", "groq_api_key", "openrouter_api_key",
            "gemini_api_key", "ollama_base_url", "custom_base_url", "search_providers",
            "tavily_api_key", "serper_api_key", "searxng_base_urls", "fetch_max_chars",
            "llm_timeout", "search_timeout",
        ):
            self.assertNotIn(gone, fields, gone)
        for prefix in ("groq", "openrouter", "gemini", "mistral", "cerebras", "ollama", "custom_",
                       "search_", "tavily", "serper", "exa_", "google_cse", "searxng", "jina"):
            self.assertFalse([name for name in fields if name.startswith(prefix)], prefix)

    def test_stale_variables_in_an_env_file_do_not_break_startup(self) -> None:
        """An old .env still loads: unknown keys are ignored, never an error."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "LLM_PROVIDER=groq\nGROQ_API_KEY=gsk_old\nSEARCH_PROVIDERS=ddg\n"
                    "ARAS_BASE_URL=http://aras.test\nARAS_TOKEN=T\nARAS_MODEL=m-1\n"
                )
            s = Settings(_env_file=path)
        self.assertTrue(s.aras_model_ready)
        self.assertFalse(hasattr(s, "llm_provider"))


# ------------------------------------------------------------------ model
class ArasModelTests(unittest.TestCase):
    def test_the_chat_url_carries_the_trailing_slash_django_needs(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=CHAT_OK)

        model = aras.build_model(aras_settings(), transport=httpx.MockTransport(handler))
        reply = run(model.complete([{"role": "user", "content": "ping"}], temperature=0.1, max_tokens=9))
        self.assertEqual(seen["url"], "http://aras.test:8101/api/v1/chat/completions/")
        self.assertEqual(seen["auth"], "Bearer SHARED")
        self.assertEqual(seen["body"]["model"], "sharif-rayen-gemma4-31b")
        self.assertEqual(seen["body"]["temperature"], 0.1)
        self.assertEqual(seen["body"]["max_tokens"], 9)
        self.assertIs(seen["body"]["stream"], False)
        self.assertNotIn("tools", seen["body"])
        self.assertEqual(reply.content, "pong")
        self.assertEqual(reply.model, "rayen-gemma4-31b")
        self.assertEqual(reply.provider, "aras")
        self.assertEqual(reply.usage, {"total_tokens": 7})
        self.assertEqual(reply.finish_reason, "stop")

    def test_the_port_is_truthy_only_when_it_can_actually_be_called(self) -> None:
        self.assertTrue(aras.build_model(aras_settings()))
        self.assertFalse(aras.build_model(Settings(_env_file=None)))
        self.assertFalse(aras.build_model(aras_settings(aras_model=None)))
        self.assertFalse(aras.build_model(aras_settings(aras_token=None)))

    def test_a_missing_model_id_is_named_and_nothing_is_sent(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=CHAT_OK)

        model = aras.build_model(aras_settings(aras_model=None), transport=httpx.MockTransport(handler))
        self.assertEqual(model.missing, ["ARAS_MODEL"])
        with self.assertRaises(aras.ArasNotConfigured) as caught:
            run(model.complete([{"role": "user", "content": "ping"}]))
        self.assertEqual(caught.exception.missing, ["ARAS_MODEL"])
        self.assertIn("ARAS_MODEL", str(caught.exception))
        self.assertIn("/aras/models", caught.exception.hint)
        self.assertEqual(calls, [], "an unconfigured router must not be called")

    def test_an_unconfigured_router_names_every_missing_variable(self) -> None:
        with self.assertRaises(aras.ArasNotConfigured) as caught:
            run(aras.build_model(Settings(_env_file=None)).complete([{"role": "user", "content": "x"}]))
        self.assertEqual(
            caught.exception.missing, ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"]
        )
        body = caught.exception.to_dict()
        self.assertEqual(body["provider"], "aras")
        self.assertEqual(body["service"], "router")
        self.assertEqual(body["missing"], ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"])

    def test_tools_are_forwarded_and_tool_calls_come_back_parsed(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "model": "m",
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {"content": None, "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "web_search", "arguments": '{"query": "aras"}'}},
                        {"id": "call_2", "type": "function",
                         "function": {"name": "web_search", "arguments": "not json"}},
                    ]},
                }],
            })

        tools = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]
        model = aras.build_model(aras_settings(), transport=httpx.MockTransport(handler))
        reply = run(model.complete([{"role": "user", "content": "find"}], tools))
        self.assertEqual(seen["body"]["tools"], tools)
        self.assertEqual(seen["body"]["tool_choice"], "auto")
        self.assertEqual(reply.content, "")
        self.assertEqual([call.name for call in reply.tool_calls], ["web_search", "web_search"])
        self.assertEqual(reply.tool_calls[0].arguments, {"query": "aras"})
        self.assertEqual(reply.tool_calls[0].id, "call_1")
        self.assertEqual(reply.tool_calls[1].arguments, {"_raw": "not json"})
        # and the reply round-trips into the next request's assistant message
        message = reply.to_assistant_message()
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"query": "aras"})

    def test_content_parts_are_joined(self) -> None:
        payload = {"choices": [{"message": {"content": [{"type": "text", "text": "po"},
                                                        {"type": "text", "text": "ng"}]}}]}
        reply = run(aras.build_model(aras_settings(), transport=json_transport(payload)).complete(
            [{"role": "user", "content": "x"}]))
        self.assertEqual(reply.content, "pong")
        # no `model` in the reply: the id that was asked for is reported
        self.assertEqual(reply.model, "sharif-rayen-gemma4-31b")

    def test_a_reply_without_choices_is_an_error_that_quotes_the_body(self) -> None:
        for payload in ({"detail": "model warming up"}, {"choices": []}, {"choices": ["x"]}):
            model = aras.build_model(aras_settings(), transport=json_transport(payload))
            with self.assertRaises(aras.ArasError) as caught:
                run(model.complete([{"role": "user", "content": "x"}]))
            self.assertNotIsInstance(caught.exception, aras.ArasNotConfigured)
            self.assertIn("without a completion", str(caught.exception))
        self.assertIn("choices", str(caught.exception))

    def test_a_refused_model_points_at_the_model_list(self) -> None:
        model = aras.build_model(
            aras_settings(aras_model="gapgpt-gpt-4o"),
            transport=json_transport({"error": {"message": "model gapgpt-gpt-4o is not allowed"}}, 403),
        )
        with self.assertRaises(aras.ArasError) as caught:
            run(model.complete([{"role": "user", "content": "x"}]))
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("not allowed", str(caught.exception))
        self.assertIn("ARAS_MODEL=gapgpt-gpt-4o", caught.exception.hint)
        self.assertIn("/aras/models", caught.exception.hint)

    def test_a_rejected_tool_request_says_the_model_cannot_call_tools(self) -> None:
        model = aras.build_model(
            aras_settings(),
            transport=json_transport({"detail": "tool calling is not supported by this model"}, 400),
        )
        with self.assertRaises(aras.ArasError) as caught:
            run(model.complete([{"role": "user", "content": "x"}], [{"type": "function"}]))
        self.assertIn("function calling", caught.exception.hint)

    def test_a_404_names_the_url_shape(self) -> None:
        model = aras.build_model(aras_settings(), transport=json_transport({"detail": "Not found."}, 404))
        with self.assertRaises(aras.ArasError) as caught:
            run(model.complete([{"role": "user", "content": "x"}]))
        self.assertIn("/api/v1", caught.exception.hint)

    def test_a_401_explains_the_router_token(self) -> None:
        model = aras.build_model(aras_settings(), transport=json_transport({"detail": "bad key"}, 401))
        with self.assertRaises(aras.ArasError) as caught:
            run(model.complete([{"role": "user", "content": "x"}]))
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("ARAS_ROUTER_TOKEN", caught.exception.hint)

    def test_a_redirect_is_an_error_never_followed(self) -> None:
        """A redirected POST loses its body — it must not look like an answer."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(301, headers={"location": "http://aras.test:8101/elsewhere/"})

        model = aras.build_model(aras_settings(), transport=httpx.MockTransport(handler))
        with self.assertRaises(aras.ArasError) as caught:
            run(model.complete([{"role": "user", "content": "x"}]))
        self.assertEqual(caught.exception.status, 301)
        self.assertIn("redirected", str(caught.exception))
        self.assertEqual(len(seen), 1, "the redirect must not be followed")

    def test_a_closed_tunnel_and_a_slow_service_get_different_hints(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with self.assertRaises(aras.ArasError) as closed:
            run(aras.build_model(aras_settings(), transport=httpx.MockTransport(refused)).complete(
                [{"role": "user", "content": "x"}]))
        self.assertIn("SSH tunnel", closed.exception.hint)
        self.assertIsNone(closed.exception.status)

        with self.assertRaises(aras.ArasError) as timed_out:
            run(aras.build_model(aras_settings(), transport=httpx.MockTransport(slow)).complete(
                [{"role": "user", "content": "x"}]))
        self.assertIn("ARAS_TIMEOUT", timed_out.exception.hint)
        self.assertNotIn("SSH tunnel", timed_out.exception.hint)

    def test_ping_reports_instead_of_raising(self) -> None:
        ok = run(aras.build_model(aras_settings(), transport=json_transport(CHAT_OK)).ping())
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["provider"], "aras")
        self.assertEqual(ok["model"], "rayen-gemma4-31b")
        self.assertEqual(ok["reply"], "pong")

        down = run(aras.build_model(aras_settings(), transport=json_transport({"detail": "x"}, 500)).ping())
        self.assertFalse(down["ok"])
        self.assertEqual(down["error"]["status"], 500)

        unset = run(aras.build_model(Settings(_env_file=None)).ping())
        self.assertFalse(unset["ok"])
        self.assertIn("ARAS_MODEL", unset["error"]["missing"])

    def test_the_model_list_reads_every_shape_the_router_may_use(self) -> None:
        shapes = (
            {"data": [{"id": "b"}, {"id": "a"}]},
            {"models": ["b", "a"]},
            {"results": [{"name": "b"}, {"model": "a"}]},
            ["b", {"id": "a"}, {"nothing": 1}, 7],
        )
        for payload in shapes:
            seen: dict[str, str] = {}

            def handler(request: httpx.Request, payload=payload) -> httpx.Response:
                seen["url"] = str(request.url)
                seen["method"] = request.method
                return httpx.Response(200, json=payload)

            model = aras.build_model(aras_settings(), transport=httpx.MockTransport(handler))
            self.assertEqual(run(model.list_models()), ["a", "b"], payload)
            self.assertEqual(seen["url"], "http://aras.test:8101/api/v1/models/")
            self.assertEqual(seen["method"], "GET")

    def test_the_model_list_works_before_a_model_is_chosen(self) -> None:
        model = aras.build_model(
            aras_settings(aras_model=None), transport=json_transport({"data": [{"id": "m-1"}]})
        )
        self.assertEqual(run(model.list_models()), ["m-1"])

    def test_describe_never_carries_the_token(self) -> None:
        described = aras.build_model(aras_settings()).describe()
        self.assertEqual(described["provider"], "aras")
        self.assertEqual(described["service"], "router")
        self.assertEqual(described["endpoint"], "http://aras.test:8101/api/v1")
        self.assertEqual(described["model"], "sharif-rayen-gemma4-31b")
        self.assertTrue(described["ready"])
        self.assertEqual(described["missing"], [])
        self.assertNotIn("SHARED", json.dumps(described))


# ----------------------------------------------------------------- search
class ArasSearchTests(unittest.TestCase):
    HITS = {"results": [
        {"title": "Python", "url": "https://python.org", "snippet": "the language", "engine": "yahoo"},
        {"title": "No url", "url": "", "snippet": "dropped"},
        {"title": "Docs", "url": "https://docs.python.org", "snippet": "the docs"},
    ]}

    def test_results_map_to_the_port_shape(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=self.HITS)

        search = aras.build_search(aras_settings(), transport=httpx.MockTransport(handler))
        response = run(search.search("python", 4))
        self.assertEqual(seen["url"], "http://aras.test:8108/v1/search/")
        self.assertEqual(seen["auth"], "Bearer SHARED")
        self.assertEqual(seen["body"], {"query": "python", "max_results": 4})
        self.assertEqual(response.provider, "aras")
        self.assertEqual(response.query, "python")
        self.assertEqual([r.url for r in response.results], ["https://python.org", "https://docs.python.org"])
        self.assertEqual(response.results[0].title, "Python")
        self.assertEqual(response.results[0].source, "aras:yahoo")
        self.assertEqual(response.results[1].source, "aras")
        body = response.to_dict()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["results"][0]["snippet"], "the language")
        self.assertEqual(
            response.results[0].as_tool_output(),
            {"title": "Python", "url": "https://python.org", "snippet": "the language"},
        )

    def test_the_limit_is_clamped_and_defaults_to_the_setting(self) -> None:
        sent: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content)["max_results"])
            return httpx.Response(200, json={"results": []})

        search = aras.build_search(
            aras_settings(agent_max_search_results=6), transport=httpx.MockTransport(handler)
        )
        for limit in (None, 0, 3, 99, "7", "nonsense"):
            run(search.search("q", limit))
        self.assertEqual(sent, [6, 6, 3, 10, 7, 6])

    def test_the_port_is_truthy_only_when_configured(self) -> None:
        self.assertTrue(aras.build_search(aras_settings()))
        self.assertFalse(aras.build_search(Settings(_env_file=None)))

    def test_a_503_is_an_error_the_caller_sees(self) -> None:
        search = aras.build_search(
            aras_settings(), transport=json_transport({"error": "engines timed out"}, 503)
        )
        with self.assertRaises(aras.ArasError) as caught:
            run(search.search("python"))
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.service, "web")
        self.assertIn("engines timed out", str(caught.exception))
        self.assertIn("engines behind it", caught.exception.hint)

    def test_unconfigured_names_the_env_vars(self) -> None:
        search = aras.build_search(Settings(_env_file=None))
        with self.assertRaises(aras.ArasNotConfigured) as caught:
            run(search.search("python"))
        self.assertIn("ARAS_WEB_TOKEN", caught.exception.hint)
        self.assertEqual(caught.exception.missing, ["ARAS_BASE_URL", "ARAS_WEB_TOKEN"])

    def test_ping_reports_results_emptiness_and_failure(self) -> None:
        ok = run(aras.build_search(aras_settings(), transport=json_transport(self.HITS)).ping("python"))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["provider"], "aras")
        self.assertEqual(ok["count"], 2)
        self.assertFalse(ok["empty"])
        self.assertEqual(ok["sample"][0]["url"], "https://python.org")

        empty = run(aras.build_search(aras_settings(), transport=json_transport({"results": []})).ping())
        self.assertFalse(empty["ok"])
        self.assertTrue(empty["empty"])
        self.assertIn("no results", empty["error"]["message"])

        down = run(aras.build_search(aras_settings(), transport=json_transport({"error": "x"}, 502)).ping())
        self.assertFalse(down["ok"])
        self.assertEqual(down["error"]["status"], 502)

    def test_describe_never_carries_the_token(self) -> None:
        described = aras.build_search(aras_settings()).describe()
        self.assertEqual(described["endpoint"], "http://aras.test:8108")
        self.assertTrue(described["ready"])
        self.assertNotIn("SHARED", json.dumps(described))


# -------------------------------------------------------------- catalogue
class ArasCatalogueTests(unittest.TestCase):
    def test_every_client_has_a_catalogue_row_and_a_health_path(self) -> None:
        rows = aras.catalogue(aras_settings())
        self.assertEqual({row["service"] for row in rows}, set(aras.CLIENTS))
        self.assertEqual(set(aras.HEALTH_PATHS), set(aras.CLIENTS))
        self.assertEqual([row["service"] for row in rows][:2], ["router", "web"])
        self.assertNotIn(8106, [row["port"] for row in rows])

    def test_a_full_block_is_ready_except_what_is_switched_off(self) -> None:
        rows = {row["service"]: row for row in aras.catalogue(aras_settings())}
        self.assertEqual(rows["router"]["state"], "ready")
        self.assertTrue(rows["router"]["required"])
        self.assertFalse(rows["web"]["required"])
        self.assertEqual(rows["router"]["endpoint"], "http://aras.test:8101/api/v1")
        self.assertEqual(rows["ingestion"]["state"], "off")  # ARAS_AUTO_INGEST defaults to false
        on = {row["service"]: row for row in aras.catalogue(aras_settings(aras_auto_ingest=True))}
        self.assertEqual(on["ingestion"]["state"], "ready")
        self.assertNotIn("SHARED", json.dumps(list(rows.values())))

    def test_an_empty_block_says_what_each_service_needs(self) -> None:
        rows = {row["service"]: row for row in aras.catalogue(Settings(_env_file=None))}
        for row in rows.values():
            self.assertEqual(row["state"], "not configured", row["service"])
            self.assertFalse(row["ready"])
            self.assertIn("ARAS_BASE_URL", row["missing_env"])
        self.assertIn("ARAS_MODEL", rows["router"]["missing_env"])

    def test_incomplete_is_told_apart_from_not_configured(self) -> None:
        rows = {row["service"]: row for row in aras.catalogue(aras_settings(aras_model=None, aras_collection=""))}
        self.assertEqual(rows["router"]["state"], "incomplete")
        self.assertEqual(rows["router"]["missing_env"], ["ARAS_MODEL"])
        self.assertEqual(rows["retrieval"]["state"], "incomplete")
        self.assertEqual(rows["web"]["state"], "ready")


# ---------------------------------------------------------------- clients
class ArasClientTests(unittest.TestCase):
    def test_ocr_sends_multipart_and_returns_text_and_pages(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith(":8102/api/v1/ocr/"))
            self.assertIn(b'filename="scan.pdf"', request.content)
            return httpx.Response(
                200, json={"text": "Hello OCR", "page_count": 1, "engine": "tesseract"}
            )

        client = aras.ArasOcrClient(aras_settings(), transport=httpx.MockTransport(handler))
        result = run(client.extract_text("scan.pdf", b"%PDF-1.4 fake"))
        self.assertEqual(result, {"text": "Hello OCR", "pages": 1, "engine": "tesseract"})

    def test_retrieval_sends_collection_mode_and_limit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertEqual(body["collection"], "alireza_notes")
            self.assertEqual(body["mode"], "hybrid")
            self.assertEqual(body["limit"], 3)
            return httpx.Response(200, json={"success": True, "results": [
                {"text": "blueharbor", "source_filename": "note.txt",
                 "document_id": "doc1", "score": 0.91}]})

        client = aras.ArasRetrievalClient(aras_settings(), transport=httpx.MockTransport(handler))
        hits = run(client.search("blueharbor", collection="alireza_notes", limit=3))
        self.assertEqual(hits[0]["source"], "note.txt")

    def test_the_data_service_gets_tenant_and_owner_headers(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["x-tenant"], "alireza-kalaie")
            self.assertEqual(request.headers["x-owner-external-id"], "alireza-kalaie")
            return httpx.Response(200, json={"answer": "42",
                                             "data": {"columns": ["n"], "rows": [[42]]}})

        backend = aras.ArasDataBackend(aras_settings(), transport=httpx.MockTransport(handler))
        result = run(backend.query("how many?"))
        self.assertEqual(result["answer"], "42")
        self.assertEqual(result["rows"], [[42]])

    def test_a_401_explains_the_token_not_the_stack(self) -> None:
        client = aras.ArasRetrievalClient(
            aras_settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"detail": "API key is inactive."})
            ),
        )
        with self.assertRaises(aras.ArasError) as caught:
            run(client.search("q", collection="c"))
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("ARAS_RETRIEVAL_TOKEN", caught.exception.hint)

    def test_status_probes_every_configured_service(self) -> None:
        report = run(aras.status(
            aras_settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "ok"})
            ),
        ))
        self.assertEqual(len(report["checked"]), len(report["configured"]))
        self.assertTrue(all(item["ok"] for item in report["checked"]))
        self.assertTrue(report["rag_ready"] and report["data_ready"])

    def test_pdf_reader_is_none_until_ocr_is_configured(self) -> None:
        self.assertIsNone(aras.pdf_reader(Settings(_env_file=None)))
        self.assertIsNotNone(aras.pdf_reader(aras_settings()))

    def test_status_skipped_names_every_service_including_the_router(self) -> None:
        """An unconfigured stack lists router too — it used to be omitted."""
        report = run(aras.status(Settings(_env_file=None)))
        self.assertIn("router", report["skipped"])
        for name in aras.CLIENTS:
            self.assertIn(name, report["skipped"])
        # and a fully configured stack skips nothing
        full = run(aras.status(
            aras_settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "ok"})
            ),
        ))
        self.assertEqual(full["skipped"], [])

    def test_health_error_carries_a_body_snippet(self) -> None:
        """HTTP 503 from *something* — the body names who actually answered.

        A VPN/system proxy answering for 127.0.0.1 returns its own error page;
        without the snippet the report is indistinguishable from a broken
        service. (This is exactly the all-seven-services-503 failure.)
        """
        client = aras.ArasRetrievalClient(
            aras_settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    503, text="<html>Access denied by security policy</html>"
                )
            ),
        )
        report = run(client.health())
        self.assertFalse(report["ok"])
        self.assertIn("HTTP 503", report["error"])
        self.assertIn("Access denied by security policy", report["error"])


# ---------------------------------------------------------- proxy bypass
class ProxyBypassTests(unittest.TestCase):
    """Loopback traffic must never be routed through HTTP(S)_PROXY.

    The field failure: a VPN/system proxy intercepted the SSH tunnel's
    127.0.0.1 ports and answered HTTP 503 for all seven services at once,
    because httpx defaults to ``trust_env=True``.
    """

    def test_trust_env_is_off_for_loopback_and_on_for_public_hosts(self) -> None:
        from app.config import trust_env_for

        for url in ("http://127.0.0.1:8101", "http://localhost:8108",
                    "https://[::1]:8000", "http://0.0.0.0:9000"):
            self.assertFalse(trust_env_for(url), url)
        for url in ("https://aras.example.com/api/v1", "http://10.0.0.5:8101"):
            self.assertTrue(trust_env_for(url), url)

    def test_aras_clients_bypass_the_proxy_env(self) -> None:
        for client in (
            aras.ArasRetrievalClient(aras_settings(aras_base_url="http://127.0.0.1")),
            aras.build_model(aras_settings(aras_base_url="http://127.0.0.1")).client,
            aras.build_search(aras_settings(aras_base_url="http://localhost")).client,
        ):
            http = client._client()
            try:
                self.assertFalse(http.trust_env, client.service)
                self.assertFalse(http.follow_redirects, client.service)
            finally:
                run(http.aclose())

    def test_a_remote_aras_host_still_honours_the_proxy_env(self) -> None:
        http = aras.build_model(aras_settings()).client._client()
        try:
            self.assertTrue(http.trust_env)
        finally:
            run(http.aclose())

    def test_hints_name_the_proxy_when_every_service_answers_an_http_error(self) -> None:
        from app.main import _hints

        settings = aras_settings()
        base = {"configured": ["router"], "rag_ready": True, "data_ready": True}
        proxied = dict(base, checked=[
            {"service": "router", "ok": False, "error": "HTTP 503 — Access denied"},
            {"service": "web", "ok": False, "error": "HTTP 503 — Access denied"},
        ])
        hints = _hints(settings, {"ok": True}, [{"provider": "aras", "ok": True}], proxied)
        self.assertTrue(any("proxy" in hint for hint in hints), hints)
        self.assertTrue(any("NO_PROXY" in hint for hint in hints), hints)
        # a closed tunnel (connection errors) keeps the tunnel hint instead
        closed = dict(base, checked=[
            {"service": "router", "ok": False, "error": "ConnectError: refused"},
        ])
        hints = _hints(settings, {"ok": True}, [{"provider": "aras", "ok": True}], closed)
        self.assertTrue(any("SSH tunnel" in hint for hint in hints), hints)

    def test_hints_say_which_variables_are_missing(self) -> None:
        from app.main import _hints

        hints = _hints(Settings(_env_file=None), {"ok": False}, [], None)
        text = " ".join(hints)
        self.assertIn("ARAS_ROUTER_TOKEN", text)
        self.assertIn("ARAS_MODEL", text)
        self.assertIn("ARAS_WEB_TOKEN", text)

    def test_a_healthy_router_that_refuses_the_call_points_at_the_request(self) -> None:
        """/health is fine but the completion failed: the fix is the model id."""
        from app.main import _hints

        healthy = {"configured": ["router", "web"], "rag_ready": True, "data_ready": True,
                   "checked": [{"service": "router", "ok": True}, {"service": "web", "ok": True}]}
        failed = {"ok": False, "error": {"message": "HTTP 403", "hint": "the router refused ARAS_MODEL=x"}}
        hints = _hints(aras_settings(), failed, [{"provider": "aras", "ok": True}], healthy)
        self.assertTrue(any("refused ARAS_MODEL=x" in hint for hint in hints), hints)
        self.assertTrue(any("/aras/models" in hint for hint in hints), hints)
        self.assertFalse(any("SSH tunnel" in hint for hint in hints), hints)

    def test_a_fully_healthy_stack_has_nothing_to_suggest_but_the_env_file(self) -> None:
        from app.main import _hints

        healthy = {"configured": ["router", "web"], "rag_ready": True, "data_ready": True,
                   "checked": [{"service": "router", "ok": True}, {"service": "web", "ok": True}]}
        hints = _hints(aras_settings(), {"ok": True}, [{"provider": "aras", "ok": True}], healthy)
        self.assertEqual([hint for hint in hints if ".env file" not in hint], [])


# ------------------------------------------------------------------ ports
class StubKnowledge:
    def __init__(self, hits=None, boom: Exception | None = None):
        self.hits = hits if hits is not None else []
        self.boom = boom
        self.queries: list[str] = []

    def describe(self):
        return {"kind": "stub", "collection": "notes"}

    async def retrieve(self, query: str, limit: int = 5):
        self.queries.append(query)
        if self.boom:
            raise self.boom
        return self.hits[:limit]


class StubDataBackend:
    def __init__(self, result=None, boom: Exception | None = None):
        self.result = result or {}
        self.boom = boom
        self.questions: list[str] = []

    def describe(self):
        return {"kind": "stub", "data_source_id": "ds-1"}

    async def query(self, question: str):
        self.questions.append(question)
        if self.boom:
            raise self.boom
        return self.result


def agent_ctx(message: str, *, knowledge=None, data_backend=None, model=None,
              attachments=None) -> AgentContext:
    return AgentContext(
        message=message,
        attachments=attachments or [],
        model_chain=model,
        search_chain=None,
        settings=Settings(_env_file=None, orchestrator_log_runs=False),
        knowledge=knowledge,
        data_backend=data_backend,
    )


class DocumentKnowledgeTests(unittest.TestCase):
    def step(self) -> PlanStep:
        return PlanStep(agent="document", task="find the refund policy", reason="test")

    def test_without_the_port_the_old_skip_behaviour_stands(self) -> None:
        outcome = run(run_document(self.step(), agent_ctx("what is the refund policy?")))
        self.assertEqual(outcome.status, "skipped")

    def test_with_the_port_the_agent_retrieves_and_cites(self) -> None:
        store = StubKnowledge(hits=[
            {"text": "Refunds are allowed within 30 days.", "source": "contract.md",
             "document_id": "doc1", "score": 0.9},
        ])
        outcome = run(run_document(
            self.step(),
            agent_ctx("what is the refund policy?", knowledge=store,
                      model=FakeModel(content="Refunds within 30 days (contract.md).")),
        ))
        self.assertEqual(outcome.status, "ok")
        self.assertIn("contract.md", outcome.sources)
        self.assertEqual(outcome.model_calls, 1)
        self.assertTrue(store.queries)

    def test_no_model_still_gives_an_extractive_grounded_answer(self) -> None:
        store = StubKnowledge(hits=[{"text": "Refunds in 30 days.", "source": "contract.md"}])
        outcome = run(run_document(self.step(), agent_ctx("refund policy?", knowledge=store)))
        self.assertEqual(outcome.status, "ok")
        self.assertIn("Refunds in 30 days.", outcome.answer)
        self.assertIn("contract.md", outcome.sources)

    def test_an_empty_store_is_a_skip_not_a_crash(self) -> None:
        outcome = run(run_document(
            self.step(), agent_ctx("anything?", knowledge=StubKnowledge(hits=[]))
        ))
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("nothing", outcome.detail)

    def test_a_down_store_is_a_failed_outcome_not_a_crash(self) -> None:
        outcome = run(run_document(
            self.step(),
            agent_ctx("anything?", knowledge=StubKnowledge(boom=RuntimeError("tunnel down"))),
        ))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("tunnel down", outcome.error)

    def test_attachments_still_win_over_the_knowledge_store(self) -> None:
        attachment = extract("contract.md", b"Refunds are allowed within 30 days.")
        store = StubKnowledge(hits=[{"text": "should not be used", "source": "x"}])
        outcome = run(run_document(
            self.step(), agent_ctx("refund policy?", knowledge=store, attachments=[attachment])
        ))
        self.assertEqual(outcome.status, "ok")
        self.assertFalse(store.queries, "the store must not be consulted when a file is attached")


class DataBackendTests(unittest.TestCase):
    def step(self, task: str = "total revenue per month") -> PlanStep:
        return PlanStep(agent="data", task=task, reason="test")

    def test_without_the_port_the_old_behaviour_stands(self) -> None:
        outcome = run(run_data(self.step(), agent_ctx("total revenue per month?")))
        self.assertEqual(outcome.status, "ok")  # computed-nothing answer, as before
        self.assertNotIn("backend", outcome.detail)

    def test_the_backend_answers_when_there_is_nothing_local(self) -> None:
        backend = StubDataBackend(result={"answer": "Revenue was 65.",
                                          "columns": ["month", "revenue"],
                                          "rows": [["Jan", 10], ["Feb", 20], ["Mar", 35]],
                                          "queries": ["SELECT ..."]})
        outcome = run(run_data(
            self.step(), agent_ctx("what was the revenue?", data_backend=backend)
        ))
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.answer, "Revenue was 65.")
        self.assertEqual(outcome.detail, "answered by the data backend")
        self.assertEqual(outcome.data["columns"], ["month", "revenue"])
        self.assertTrue(backend.questions)

    def test_local_arithmetic_still_wins_over_the_backend(self) -> None:
        backend = StubDataBackend(result={"answer": "should not be used"})
        outcome = run(run_data(
            PlanStep(agent="data", task="compute", reason="test"),
            agent_ctx("12 + 30 + 45", data_backend=backend),
        ))
        self.assertEqual(outcome.status, "ok")
        self.assertFalse(backend.questions, "local computation must stay first")

    def test_a_down_backend_is_a_failed_outcome_not_a_crash(self) -> None:
        outcome = run(run_data(
            self.step(), agent_ctx("what was the revenue?",
                                   data_backend=StubDataBackend(boom=RuntimeError("503"))),
        ))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("503", outcome.error)

    def test_an_empty_answer_is_a_skip(self) -> None:
        outcome = run(run_data(
            self.step(), agent_ctx("what was the revenue?",
                                   data_backend=StubDataBackend(result={"answer": "", "rows": []})),
        ))
        self.assertEqual(outcome.status, "skipped")


# ---------------------------------------------------------------- uploads
class PdfReaderInjectionTests(unittest.TestCase):
    def test_the_injected_reader_is_used_for_pdfs(self) -> None:
        def reader(name: str, data: bytes):
            return "OCR text from the service", 3

        attachment = extract("scan.pdf", b"%PDF-1.4 fake", pdf_reader=reader)
        self.assertEqual(attachment.text, "OCR text from the service")
        self.assertEqual(attachment.pages, 3)

    def test_a_failing_reader_falls_back_to_the_local_parse(self) -> None:
        def reader(name: str, data: bytes):
            raise RuntimeError("service down")

        # the local parse also fails on fake bytes — but it must be the local
        # error (the honest one about the file), not the reader's crash
        with self.assertRaises(Exception) as caught:
            extract("scan.pdf", b"%PDF-1.4 fake", pdf_reader=reader)
        self.assertNotIn("service down", str(caught.exception))

    def test_non_pdfs_never_touch_the_reader(self) -> None:
        def reader(name: str, data: bytes):
            raise AssertionError("must not be called")

        attachment = extract("note.txt", b"plain text", pdf_reader=reader)
        self.assertEqual(attachment.text, "plain text")


if __name__ == "__main__":
    unittest.main()
