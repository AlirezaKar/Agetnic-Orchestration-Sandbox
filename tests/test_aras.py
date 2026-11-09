"""The ARAS wiring: config, the router preset, the web backend, the two ports.

Everything here runs against ``httpx.MockTransport`` — no tunnel, no token,
no network. What is being proven, per block:

* config     — URL derivation, token fallback, the chains that re-order
               themselves when ARAS is configured (and only then)
* llm        — the router is OpenAI-compatible *with a trailing slash*; a
               POST to ``.../chat/completions`` (no slash) would die on the
               301 redirect, so the exact URL is asserted
* search     — the web service leads the chain, 503 falls through, and an
               unconfigured provider says which env vars are missing
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
from app.llm.registry import build_chain
from app.orchestrator.agents import AgentContext, run_data, run_document
from app.orchestrator.files import extract
from app.orchestrator.planner import PlanStep
from app.search.base import SearchError
from app.search.providers import ArasWebSearch
from app.search.registry import build_search_chain

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
        self.assertFalse(s.aras_rag_ready)
        self.assertFalse(s.aras_data_ready)
        self.assertNotIn("aras", s.search_chain)
        chain = build_chain(s)
        self.assertNotIn("aras", [p.name for p in chain.providers])

    def test_the_router_leads_the_model_chain_when_configured(self) -> None:
        chain = build_chain(aras_settings())
        self.assertEqual(chain.providers[0].name, "aras")

    def test_the_web_service_leads_the_search_chain_when_configured(self) -> None:
        self.assertEqual(aras_settings().search_chain[0], "aras")

    def test_an_explicit_search_providers_list_is_not_rewritten(self) -> None:
        s = aras_settings(search_providers="ddg,wikipedia")
        self.assertEqual(s.search_chain, ["ddg", "wikipedia"])

    def test_readiness_needs_every_field_it_claims_to_need(self) -> None:
        self.assertTrue(aras_settings().aras_rag_ready)
        self.assertTrue(aras_settings().aras_data_ready)
        self.assertFalse(aras_settings(aras_collection="").aras_rag_ready)
        self.assertFalse(aras_settings(aras_data_source_id=None).aras_data_ready)
        self.assertFalse(aras_settings(aras_tenant=None).aras_data_ready)

    def test_the_public_summary_names_services_but_never_tokens(self) -> None:
        summary = aras_settings().public_summary()
        self.assertIn("router", summary["aras_services"])
        self.assertNotIn("SHARED", json.dumps(summary))


# -------------------------------------------------------------------- llm
class ArasRouterTests(unittest.TestCase):
    def test_the_chat_url_carries_the_trailing_slash_django_needs(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "pong"}}],
                    "model": "rayen-gemma4-31b",
                },
            )

        provider = build_chain(aras_settings()).providers[0]
        provider._transport = httpx.MockTransport(handler)
        reply = run(provider.complete([{"role": "user", "content": "ping"}]))
        self.assertEqual(seen["url"], "http://aras.test:8101/api/v1/chat/completions/")
        self.assertEqual(seen["auth"], "Bearer SHARED")
        self.assertEqual(seen["body"]["model"], "sharif-rayen-gemma4-31b")
        self.assertEqual(reply.content, "pong")
        self.assertEqual(reply.model, "rayen-gemma4-31b")

    def test_a_missing_model_id_keeps_the_router_out_of_the_chain(self) -> None:
        chain = build_chain(aras_settings(aras_model=None))
        self.assertNotIn("aras", [p.name for p in chain.providers])
        self.assertTrue(any("ARAS_MODEL" in skip for skip in chain.skipped))


# ----------------------------------------------------------------- search
class ArasWebSearchTests(unittest.TestCase):
    def transport(self, payload: dict[str, Any], status: int = 200) -> httpx.MockTransport:
        return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))

    def test_results_map_to_the_chain_shape(self) -> None:
        provider = ArasWebSearch(
            aras_settings(),
            transport=self.transport(
                {"results": [{"title": "Python", "url": "https://python.org",
                              "snippet": "the language", "engine": "yahoo"}]}
            ),
        )
        results = run(provider.search("python"))
        self.assertEqual(results[0].title, "Python")
        self.assertEqual(results[0].source, "aras:yahoo")

    def test_a_503_is_a_search_error_so_the_chain_falls_through(self) -> None:
        provider = ArasWebSearch(
            aras_settings(), transport=self.transport({"error": "engines timed out"}, 503)
        )
        with self.assertRaises(SearchError):
            run(provider.search("python"))

    def test_unconfigured_names_the_env_vars(self) -> None:
        provider = ArasWebSearch(Settings(_env_file=None))
        with self.assertRaises(SearchError) as caught:
            run(provider.search("python"))
        self.assertIn("ARAS_WEB_TOKEN", caught.exception.hint)

    def test_the_built_chain_uses_aras_first_and_wins(self) -> None:
        chain = build_search_chain(
            aras_settings(),
            transport=self.transport(
                {"results": [{"title": "t", "url": "https://x.test/a", "snippet": "s"}]}
            ),
        )
        self.assertEqual(chain.providers[0].name, "aras")
        response = run(chain.search("query"))
        self.assertEqual(response.provider, "aras")


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

        for url in ("http://127.0.0.1:8101", "http://localhost:11434",
                    "https://[::1]:8000", "http://0.0.0.0:9000"):
            self.assertFalse(trust_env_for(url), url)
        for url in ("https://api.groq.com/openai/v1", "https://openrouter.ai/api/v1"):
            self.assertTrue(trust_env_for(url), url)

    def test_aras_clients_bypass_the_proxy_env(self) -> None:
        client = aras.ArasRetrievalClient(aras_settings(aras_base_url="http://127.0.0.1"))
        http = client._client()
        try:
            self.assertFalse(http.trust_env)
        finally:
            run(http.aclose())

    def test_openai_compat_bypasses_the_proxy_env_only_on_loopback(self) -> None:
        from app.llm.openai_compat import OpenAICompatProvider

        local = OpenAICompatProvider(
            name="ollama", label="Ollama", base_url="http://127.0.0.1:11434/v1",
            api_key="x", model="m", auth_scheme="none",
        )
        public = OpenAICompatProvider(
            name="groq", label="Groq", base_url="https://api.groq.com/openai/v1",
            api_key="x", model="m",
        )
        local_http, public_http = local._client(), public._client()
        try:
            self.assertFalse(local_http.trust_env)
            self.assertTrue(public_http.trust_env)
        finally:
            run(local_http.aclose())
            run(public_http.aclose())

    def test_hints_name_the_proxy_when_every_service_answers_an_http_error(self) -> None:
        from app.main import _hints

        settings = Settings(_env_file=None)
        base = {"configured": ["router"], "rag_ready": True, "data_ready": True}
        proxied = dict(base, checked=[
            {"service": "router", "ok": False, "error": "HTTP 503 — Access denied"},
            {"service": "web", "ok": False, "error": "HTTP 503 — Access denied"},
        ])
        hints = _hints(settings, {"ok": True}, [{"provider": "ddg", "ok": True}], proxied)
        self.assertTrue(any("proxy" in hint for hint in hints), hints)
        self.assertTrue(any("NO_PROXY" in hint for hint in hints), hints)
        # a closed tunnel (connection errors) keeps the tunnel hint instead
        closed = dict(base, checked=[
            {"service": "router", "ok": False, "error": "ConnectError: refused"},
        ])
        hints = _hints(settings, {"ok": True}, [{"provider": "ddg", "ok": True}], closed)
        self.assertTrue(any("SSH tunnel" in hint for hint in hints), hints)


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
