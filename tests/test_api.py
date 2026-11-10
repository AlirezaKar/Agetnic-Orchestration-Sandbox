"""HTTP surface tests.

Two halves of the same promise: with an empty ``ARAS_*`` block the app fails
loudly and says which variables to set (never a canned answer, never another
provider); with a configured block every route goes to the ARAS services and
nowhere else.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from fastapi.testclient import TestClient

import app.main as main_module
from app import aras
from app.config import Settings, get_settings
from app.main import VERSION, app

#: Every ARAS field pinned to "unset", so a developer's own .env or shell
#: variables can never turn these into live calls.
BLANK_ARAS: dict[str, Any] = {
    "aras_base_url": "",
    "aras_token": None,
    "aras_model": None,
    "aras_collection": "",
    "aras_tenant": None,
    "aras_owner": None,
    "aras_data_source_id": None,
    **{f"aras_{name}_url": None for name in Settings.ARAS_PORTS},
    **{f"aras_{name}_token": None for name in Settings.ARAS_PORTS},
}


class _HttpCase(unittest.TestCase):
    """A client on the real app, with its settings and stores in a temp folder."""

    overrides: dict[str, Any] = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.settings = Settings(
            _env_file=None,
            **{
                **BLANK_ARAS,
                # the harness memory and the file store are real: keep them out of the repo
                "memory_dir": str(root / "memory"),
                "files_dir": str(root / "files"),
                "orchestrator_log_runs": False,
                **cls.overrides,
            },
        )
        get_settings.cache_clear()
        main_module.reset_store()
        main_module.reset_memory()
        cls._patch = mock.patch.object(main_module, "get_settings", lambda: cls.settings)
        cls._patch.start()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls._patch.stop()
        main_module.reset_store()
        main_module.reset_memory()
        get_settings.cache_clear()
        cls._tmp.cleanup()


class ApiTests(_HttpCase):
    """Nothing configured: the page works, every model route says what is missing."""

    # ------------------------------------------------------------- basics
    def test_index_serves_test_harness(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Agetnic Orchestration Sandbox", response.text)
        self.assertIn("Run diagnostics", response.text)
        self.assertIn("assets/js/app.js", response.text)
        self.assertIn('id="mode-list"', response.text)
        self.assertIn('id="diag-summary"', response.text)
        self.assertIn('id="trace-json"', response.text)

    def test_static_assets_are_served(self):
        for path, marker in (
            ("/assets/css/styles.css", ".topbar"),
            ("/assets/css/extra.css", ".diag-row"),
            ("/assets/css/theme.css", 'html[data-theme="light"]'),
            ("/assets/js/app.js", "runDiagnostics"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.text, path)

    def test_version_endpoint_matches_the_page(self):
        for path in ("/version", "/api/version"):
            payload = self.client.get(path).json()
            self.assertEqual(payload["version"], VERSION, path)
            self.assertTrue(payload["changelog"].endswith(f"v{VERSION}.md"), path)
            self.assertTrue(payload["logs"].endswith("logs"), path)
            # the advertised changelog must exist, not just look right
            root = Path(__file__).resolve().parents[1]
            self.assertTrue((root / payload["changelog"]).is_file(), payload["changelog"])
        self.assertIn(f'content="{VERSION}"', self.client.get("/").text)

    def test_index_is_never_cached(self):
        response = self.client.get("/")
        self.assertIn("no-store", response.headers.get("cache-control", ""))

    def test_ui_redirect_is_relative(self):
        response = self.client.get("/ui", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], ".")

    def test_head_requests_are_answered(self):
        self.assertEqual(self.client.head("/").status_code, 200)
        self.assertEqual(self.client.head("/health").status_code, 200)
        self.assertEqual(self.client.head("/api/health").status_code, 200)

    def test_head_on_unknown_route_has_no_body(self):
        response = self.client.head("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content, b"")

    def test_favicon_does_not_404(self):
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)

    # ------------------------------------------------------------- health
    def test_health_names_what_the_model_and_the_search_are_missing(self):
        data = self.client.get("/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["model_provider_ready"])
        self.assertEqual(data["model_chain"], [])
        self.assertEqual(data["search_chain"], [])
        self.assertEqual(data["model"]["provider"], "aras")
        self.assertEqual(data["model"]["service"], "router")
        self.assertFalse(data["model"]["ready"])
        self.assertEqual(
            data["model"]["missing"], ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"]
        )
        self.assertEqual(data["search"]["service"], "web")
        self.assertEqual(data["search"]["missing"], ["ARAS_BASE_URL", "ARAS_WEB_TOKEN"])
        self.assertEqual(data["aras"]["configured"], [])

    def test_health_describes_the_orchestration_pipeline(self):
        data = self.client.get("/health").json()
        orchestrator = data["orchestrator"]
        for key in ("enabled", "planner", "evaluate", "optimize", "review", "review_rounds", "agents"):
            self.assertIn(key, orchestrator)
        self.assertTrue(orchestrator["evaluate"])  # the gate is on by default
        self.assertTrue(orchestrator["optimize"])
        self.assertGreaterEqual(orchestrator["review_rounds"], 1)

    def test_config_hides_secrets(self):
        data = self.client.get("/config").json()
        flattened = str(data).lower()
        self.assertNotIn("api_key=", flattened)
        # no field that could hold a credential is part of the summary at all
        self.assertEqual([key for key in data if key.endswith(("_token", "_key", "_secret"))], [])
        self.assertEqual(data["model_provider"], "aras")
        self.assertEqual(data["search_provider"], "aras")
        self.assertFalse(data["model_ready"])
        self.assertNotIn("search_chain", data)
        self.assertNotIn("llm_chain", data)

    # ----------------------------------------------------- ARAS services
    def test_providers_lists_the_aras_services_and_nothing_else(self):
        data = self.client.get("/providers").json()
        self.assertEqual(set(data), {"version", "model", "search", "services", "env_file"})
        names = [row["service"] for row in data["services"]]
        self.assertEqual(names, ["router", "web", "ocr", "retrieval", "ingestion", "data", "vector"])
        self.assertNotIn("providers", data)
        self.assertNotIn("core", data)
        text = json.dumps(data).lower()
        for gone in ("groq", "gemini", "openrouter", "ollama", "duckduckgo", "tavily", "wikipedia", "mock"):
            self.assertNotIn(gone, text, gone)

    def test_each_service_row_says_what_it_is_for_and_what_it_needs(self):
        rows = {row["service"]: row for row in self.client.get("/providers").json()["services"]}
        for row in rows.values():
            for key in ("label", "port", "role", "route", "endpoint", "state", "ready",
                        "configured", "required", "env", "missing_env"):
                self.assertIn(key, row, (row["service"], key))
            self.assertEqual(row["state"], "not configured")
            self.assertTrue(row["role"])
        self.assertEqual(rows["router"]["port"], 8101)
        self.assertEqual(rows["web"]["port"], 8108)
        self.assertTrue(rows["router"]["required"])
        self.assertIn("ARAS_MODEL", rows["router"]["missing_env"])
        self.assertNotIn(8106, [row["port"] for row in rows.values()])

    def test_the_old_provider_routes_are_gone(self):
        self.assertEqual(self.client.get("/providers/groq/models").status_code, 404)
        self.assertEqual(self.client.get("/search/diagnose").status_code, 404)
        listed = " ".join(self.client.get("/api").json()["routes"])
        self.assertNotIn("/providers/{name}/models", listed)
        self.assertNotIn("/search/diagnose", listed)
        self.assertIn("/aras/models", listed)

    def test_the_model_list_needs_the_router_and_says_so(self):
        response = self.client.get("/aras/models")
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "configuration")
        # listing needs URL + token only — not a model id, that is what it is for
        self.assertEqual(body["detail"]["missing"], ["ARAS_BASE_URL", "ARAS_ROUTER_TOKEN"])
        self.assertEqual(body["detail"]["service"], "router")

    def test_aras_status_lists_every_service_as_skipped(self):
        data = self.client.get("/aras").json()
        self.assertEqual(data["configured"], [])
        self.assertEqual(data["checked"], [])
        self.assertEqual(sorted(data["skipped"]), sorted(aras.CLIENTS))
        self.assertFalse(data["model_ready"])
        self.assertFalse(data["search_ready"])
        self.assertIn("ARAS_MODEL", data["missing"]["router"])
        self.assertEqual(data["endpoints"], {})
        self.assertEqual(data["version"], VERSION)

    # ------------------------------------------------------------- search
    def test_search_without_the_web_service_returns_actionable_400(self):
        response = self.client.post("/search", json={"query": "x"})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "configuration")
        self.assertIn("ARAS_WEB_TOKEN", body["message"])
        self.assertEqual(body["detail"]["missing"], ["ARAS_BASE_URL", "ARAS_WEB_TOKEN"])
        self.assertIn(".env", body["detail"]["hint"])

    def test_search_ignores_a_provider_field_it_no_longer_has(self):
        """There is one backend; an old client naming another gets the same answer."""
        response = self.client.post("/search", json={"query": "x", "provider": "ddg"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["service"], "web")

    def test_search_requires_query(self):
        response = self.client.post("/search", json={"query": ""})
        self.assertEqual(response.status_code, 422)

    # -------------------------------------------------------------- agent
    def test_chat_without_the_router_returns_actionable_400(self):
        response = self.client.post("/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "configuration")
        for env in ("ARAS_BASE_URL", "ARAS_ROUTER_TOKEN", "ARAS_MODEL"):
            self.assertIn(env, body["message"])
        self.assertEqual(body["detail"]["provider"], "aras")
        self.assertEqual(body["detail"]["service"], "router")
        self.assertIn("/aras/models", body["detail"]["hint"])
        self.assertNotIn("answer", body)

    def test_chat_validates_message(self):
        response = self.client.post("/chat", json={"message": ""})
        self.assertEqual(response.status_code, 422)

    def test_orchestrate_without_the_router_still_explains_itself(self):
        """Auto mode never 500s on an empty block: a run, a trace, a clear reason."""
        response = self.client.post("/orchestrate", json={"message": "What is an agent?"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["run_id"])
        text = json.dumps(body)
        self.assertIn("No model is configured", text)
        self.assertIn("ARAS_ROUTER_TOKEN", text)
        self.assertNotIn("LLM_PROVIDER", text)

    def test_agents_route_reports_that_nothing_is_wired(self):
        data = self.client.get("/agents").json()
        self.assertFalse(data["model_ready"])
        self.assertFalse(data["search_ready"])

    # ------------------------------------------------------------ routing
    def test_api_prefix_mirrors_the_root_routes(self):
        root = self.client.get("/health").json()
        prefixed = self.client.get("/api/health").json()
        self.assertEqual(root["version"], prefixed["version"])
        self.assertEqual(root["search_chain"], prefixed["search_chain"])
        self.assertEqual(root["model"], prefixed["model"])

    def test_api_prefix_chat_works_instead_of_404(self):
        response = self.client.post("/api/chat", json={"message": "hello"})
        # nothing configured -> the same actionable 400 as the unprefixed route
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "configuration")

    def test_api_root_lists_routes(self):
        data = self.client.get("/api").json()
        self.assertIn("version", data)
        self.assertTrue(any("/chat" in route for route in data["routes"]))

    def test_every_listed_route_exists(self):
        """API_ROUTES is what the 404 page and run.py print: it must not drift."""
        import re

        def shape(path: str) -> str:  # /runs/{run_id} and /runs/{id} are the same route
            return re.sub(r"\{[^}]*\}", "{}", path)

        served = {
            (method.upper(), shape(path))
            for path, operations in self.client.get("/openapi.json").json()["paths"].items()
            for method in operations
        }
        listed = set()
        for line in main_module.API_ROUTES:
            method, path = line.split()[:2]
            if path == "/" or "…" in path:  # the page and the static mount: not API routes
                continue
            listed.add((method, shape(path)))
            self.assertIn((method, shape(path)), served, line)
        # ...and the other way round: a route that is served is on the list
        self.assertEqual(sorted(served - listed), [])

    def test_unknown_route_returns_explanation(self):
        response = self.client.get("/definitely-not-a-route")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"], "not_found")
        self.assertIn("hint", body)
        self.assertTrue(any("/chat" in route for route in body["routes"]))

    def test_unknown_api_route_explains_the_prefix(self):
        body = self.client.post("/api/nope", json={}).json()
        self.assertEqual(body["error"], "not_found")
        self.assertIn("/api", body["hint"])

    # ----------------------------------------------------------- diagnose
    def test_diagnose_returns_hints(self):
        data = self.client.get("/diagnose").json()
        self.assertFalse(data["model"]["ok"])
        self.assertEqual(data["model"]["provider"], "aras")
        self.assertIn("ARAS_MODEL", data["model"]["error"]["missing"])
        self.assertEqual(data["search"]["checked"], [])
        self.assertEqual(data["search"]["skipped"][0]["provider"], "aras")
        self.assertIn("ARAS_WEB_TOKEN", data["search"]["skipped"][0]["reason"])
        self.assertIsNone(data["aras"])
        hints = " ".join(data["hints"])
        self.assertIn("ARAS_ROUTER_TOKEN", hints)
        self.assertIn("ARAS_WEB_TOKEN", hints)
        self.assertNotIn("LLM_PROVIDER", hints)


class ArasConfiguredApiTests(_HttpCase):
    """A filled block: every route reaches the ARAS services (mocked at HTTP)."""

    overrides = {
        "aras_base_url": "http://aras.test",
        "aras_token": "SHARED-SECRET",
        "aras_model": "sharif-rayen-gemma4-31b",
        "aras_collection": "alireza_notes",
        "aras_tenant": "alireza-kalaie",
        "aras_owner": "alireza-kalaie",
        "aras_data_source_id": "ds-1",
        "orchestrator_planner": "heuristic",
        "orchestrator_review": False,
        "orchestrator_evaluate": False,
    }

    def setUp(self):
        self.calls: list[tuple[str, str]] = []
        #: a test sets this to answer some requests itself (None = the default below)
        self.override: Any = None

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.method, str(request.url)))
            port = request.url.port
            if self.override is not None:
                answer = self.override(request)
                if answer is not None:
                    return answer
            if request.url.path.rstrip("/").endswith("health"):
                return httpx.Response(200, json={"status": "ok"})
            if request.headers.get("authorization") != "Bearer SHARED-SECRET":
                return httpx.Response(401, json={"detail": "bad token"})
            if port == 8101 and request.url.path.endswith("/models/"):
                return httpx.Response(200, json={"data": [{"id": "sharif-rayen-gemma4-31b"}, {"id": "a-model"}]})
            if port == 8101:
                return httpx.Response(200, json={
                    "model": "rayen-gemma4-31b",
                    "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 5},
                })
            if port == 8108:
                return httpx.Response(200, json={"results": [
                    {"title": "ARAS", "url": "https://aras.example/a", "snippet": "s", "engine": "bing"},
                ]})
            return httpx.Response(404, json={"detail": "unexpected call"})

        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = transport
            return real(*args, **kwargs)

        patcher = mock.patch.object(aras.httpx, "AsyncClient", client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def urls(self) -> list[str]:
        return [url for _, url in self.calls]

    def test_health_shows_the_router_and_the_web_service_ready(self):
        data = self.client.get("/health").json()
        self.assertTrue(data["model_provider_ready"])
        self.assertEqual(data["model_chain"], ["aras"])
        self.assertEqual(data["search_chain"], ["aras"])
        self.assertEqual(data["model"]["model"], "sharif-rayen-gemma4-31b")
        self.assertEqual(data["model"]["endpoint"], "http://aras.test:8101/api/v1")
        self.assertEqual(data["model"]["missing"], [])
        self.assertEqual(self.calls, [], "/health is static: it must not call a service")

    def test_no_route_ever_returns_the_token(self):
        for path in ("/health", "/config", "/providers", "/aras", "/aras/models", "/diagnose", "/agents"):
            self.assertNotIn("SHARED-SECRET", self.client.get(path).text, path)

    def test_chat_is_one_router_completion(self):
        response = self.client.post("/chat", json={"message": "ping", "allow_search": False})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["answer"], "pong")
        self.assertEqual(body["provider"], "aras")
        self.assertEqual(body["model"], "rayen-gemma4-31b")
        self.assertEqual(self.calls, [("POST", "http://aras.test:8101/api/v1/chat/completions/")])

    def test_search_is_one_web_call(self):
        response = self.client.post("/search", json={"query": "aras", "max_results": 3})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["provider"], "aras")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["url"], "https://aras.example/a")
        self.assertEqual(body["results"][0]["source"], "aras:bing")
        self.assertEqual(self.calls, [("POST", "http://aras.test:8108/v1/search/")])

    def test_a_router_failure_is_a_502_that_carries_the_hint(self):
        self.override = lambda request: (
            httpx.Response(403, json={"error": {"message": "model not allowed for this key"}})
            if request.url.port == 8101 else None
        )
        response = self.client.post("/chat", json={"message": "ping", "allow_search": False})
        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["error"], "aras")
        self.assertEqual(body["detail"]["service"], "router")
        self.assertEqual(body["detail"]["status"], 403)
        self.assertIn("model not allowed", body["message"])
        self.assertIn("/aras/models", body["detail"]["hint"])

    def test_a_web_failure_is_a_502_not_a_silent_fallback(self):
        self.override = lambda request: (
            httpx.Response(503, json={"error": "engines timed out"})
            if request.url.port == 8108 else None
        )
        response = self.client.post("/search", json={"query": "aras"})
        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["error"], "aras")
        self.assertEqual(body["detail"]["service"], "web")
        self.assertEqual(body["detail"]["status"], 503)
        self.assertEqual(self.urls(), ["http://aras.test:8108/v1/search/"])

    def test_the_model_list_comes_from_the_router(self):
        body = self.client.get("/aras/models").json()
        self.assertEqual(body["models"], ["a-model", "sharif-rayen-gemma4-31b"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["configured_model"], "sharif-rayen-gemma4-31b")
        self.assertEqual(body["endpoint"], "http://aras.test:8101/api/v1")
        self.assertEqual(self.calls, [("GET", "http://aras.test:8101/api/v1/models/")])

    def test_aras_status_probes_all_seven_services(self):
        data = self.client.get("/aras").json()
        self.assertEqual(len(data["checked"]), 7)
        self.assertTrue(all(item["ok"] for item in data["checked"]))
        self.assertEqual(data["skipped"], [])
        self.assertTrue(data["model_ready"])
        self.assertTrue(data["search_ready"])
        self.assertEqual(data["model"], "sharif-rayen-gemma4-31b")
        self.assertEqual({httpx.URL(url).port for url in self.urls()},
                         {8101, 8102, 8103, 8104, 8105, 8107, 8108})
        self.assertEqual(data["endpoints"]["router"], "http://aras.test:8101/api/v1")

    def test_diagnose_runs_a_completion_a_search_and_every_health_probe(self):
        data = self.client.get("/diagnose", params={"q": "aras"}).json()
        self.assertTrue(data["model"]["ok"])
        self.assertEqual(data["model"]["reply"], "pong")
        self.assertEqual(data["model"]["model"], "rayen-gemma4-31b")
        self.assertEqual(data["search"]["ok"], ["aras"])
        self.assertEqual(data["search"]["failed"], [])
        self.assertEqual(data["search"]["skipped"], [])
        self.assertEqual(data["search"]["checked"][0]["count"], 1)
        self.assertEqual(len(data["aras"]["checked"]), 7)
        self.assertIn("http://aras.test:8101/api/v1/chat/completions/", self.urls())
        self.assertIn("http://aras.test:8108/v1/search/", self.urls())
        self.assertEqual([hint for hint in data["hints"] if ".env file" not in hint], [])

    def test_diagnose_tells_a_refused_model_apart_from_a_closed_tunnel(self):
        """Every /health answers, the completion is refused: the fix is the model id."""
        self.override = lambda request: (
            httpx.Response(403, json={"error": {"message": "model not allowed for this key"}})
            if request.url.path.endswith("/chat/completions/") else None
        )
        data = self.client.get("/diagnose").json()
        self.assertFalse(data["model"]["ok"])
        self.assertEqual(data["model"]["error"]["status"], 403)
        self.assertTrue(all(item["ok"] for item in data["aras"]["checked"]))
        hints = " ".join(data["hints"])
        self.assertIn("did not answer the test completion", hints)
        self.assertIn("/aras/models", hints)
        self.assertNotIn("SSH tunnel", hints)

    def test_diagnose_names_the_tunnel_when_nothing_answers(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        self.override = refuse
        data = self.client.get("/diagnose").json()
        self.assertFalse(data["model"]["ok"])
        self.assertEqual(data["search"]["failed"], ["aras"])
        self.assertFalse(any(item["ok"] for item in data["aras"]["checked"]))
        hints = " ".join(data["hints"])
        self.assertIn("SSH tunnel", hints)
        self.assertIn("aras_smoke.py", hints)
        # one cause, one hint: the failed calls are not reported a second time
        self.assertNotIn("did not answer the test completion", hints)
        self.assertNotIn("did not answer the test search", hints)

    def test_orchestrate_answers_through_the_router(self):
        response = self.client.post("/orchestrate", json={"message": "What is an agent?"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["provider"], "aras")
        self.assertEqual(body["model"], "rayen-gemma4-31b")
        self.assertIn("pong", body["answer"])
        self.assertTrue(self.urls())
        self.assertEqual(set(self.urls()), {"http://aras.test:8101/api/v1/chat/completions/"})

    def test_agents_route_reports_both_ports_ready(self):
        data = self.client.get("/agents").json()
        self.assertTrue(data["model_ready"])
        self.assertTrue(data["search_ready"])



class FrontendContractTests(unittest.TestCase):
    """The static UI and the backend must agree on paths and versions."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        cls.root = root
        cls.index = (root / "app/static/index.html").read_text(encoding="utf-8")
        cls.app_js = (root / "app/static/assets/js/app.js").read_text(encoding="utf-8")
        cls.styles = (root / "app/static/assets/css/styles.css").read_text(encoding="utf-8")
        cls.extra = (root / "app/static/assets/css/extra.css").read_text(encoding="utf-8")
        cls.theme = (root / "app/static/assets/css/theme.css").read_text(encoding="utf-8")

    def rules(self, css: str, selector: str) -> list[str]:
        """Every declaration block for one selector, comments removed."""
        import re

        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        out = []
        for selectors, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
            names = [" ".join(part.split()) for part in selectors.split(",")]
            if selector in names:
                out.append(body)
        return out

    def test_version_module_is_the_only_source(self):
        from app.version import VERSION as CANONICAL

        self.assertEqual(CANONICAL, VERSION)
        self.assertIn(f'content="{CANONICAL}"', self.index)
        self.assertNotIn("0.4.", self.index)

    def test_assets_are_referenced_relatively(self):
        # absolute "/assets/..." breaks under any proxy prefix -> unstyled page
        self.assertIn('href="assets/css/styles.css', self.index)
        self.assertIn('href="assets/css/extra.css', self.index)
        self.assertIn('href="assets/css/theme.css', self.index)
        self.assertIn('src="assets/js/app.js', self.index)
        self.assertNotIn('href="/assets/', self.index)
        self.assertNotIn('src="/assets/', self.index)

    def test_asset_urls_are_cache_busted_with_the_app_version(self):
        self.assertIn(f"styles.css?v={VERSION}", self.index)
        self.assertIn(f"extra.css?v={VERSION}", self.index)
        self.assertIn(f"theme.css?v={VERSION}", self.index)
        self.assertIn(f"app.js?v={VERSION}", self.index)
        self.assertIn(f'content="{VERSION}"', self.index)

    def test_frontend_negotiates_the_api_base(self):
        for marker in ("candidateBases", "negotiate", "apiUrl", "conn-banner"):
            self.assertIn(marker, self.app_js, marker)

    def test_frontend_never_hardcodes_root_api_paths(self):
        for bad in ('fetch("/chat"', 'fetch("/health"', 'fetch("/providers"'):
            self.assertNotIn(bad, self.app_js)

    def test_frontend_surfaces_connection_problems(self):
        self.assertIn("Backend not reachable", self.app_js)
        self.assertIn("Styles failed to load", self.app_js)

    # ── the light theme ────────────────────────────────────────────────
    def test_the_light_theme_only_ever_targets_the_light_root(self):
        """theme.css is loaded after everything else: an unscoped rule would
        repaint the dark design, so every selector must be prefixed."""
        import re

        css = re.sub(r"/\*.*?\*/", "", self.theme, flags=re.S)
        selectors = []
        for chunk in css.split("}"):
            head = chunk.split("{")[0].strip()
            if head and "{" in chunk:
                selectors.extend(part.strip() for part in head.split(","))
        self.assertGreater(len(selectors), 100)
        stray = [sel for sel in selectors if not sel.startswith('html[data-theme="light"]')]
        self.assertEqual(stray, [])

    def test_the_light_theme_restates_every_core_token(self):
        import re

        block = re.search(r'html\[data-theme="light"\]\s*\{([^}]*)\}', self.theme, flags=re.S)
        self.assertIsNotNone(block)
        tokens = block.group(1)
        for token in ("--bg", "--surface", "--text", "--accent", "--mint", "--amber", "--red"):
            self.assertIn(f"{token}:", tokens, token)
        self.assertIn("color-scheme: light", tokens)

    def test_the_page_starts_dark_and_can_flip_before_paint(self):
        # an inline script in <head> applies the stored choice before first paint
        head = self.index.split("</head>")[0]
        self.assertIn('data-theme="dark"', head)
        self.assertIn("agetnic.theme.v1", head)
        self.assertIn("dataset.theme", head)
        self.assertIn('id="btn-theme"', self.index)
        self.assertIn('aria-pressed="false"', self.index)

    def test_the_theme_choice_is_remembered(self):
        self.assertIn("agetnic.theme.v1", self.app_js)
        self.assertIn('name="theme-color"', self.index)
        self.assertIn("#f2f5fb", self.app_js)

    # ── the resizable columns ──────────────────────────────────────────
    def test_the_workspace_has_two_drag_handles(self):
        self.assertEqual(self.index.count('role="separator"'), 2)
        self.assertIn('id="gutter-left"', self.index)
        self.assertIn('id="gutter-right"', self.index)
        self.assertIn('aria-orientation="vertical"', self.index)
        # the range itself comes from the JS layout spec, the markup stays static
        self.assertIn('class="gutter gutter-left"', self.index)
        self.assertIn('class="gutter gutter-right"', self.index)

    def test_the_columns_are_driven_by_custom_properties(self):
        self.assertIn("--sidebar-width", self.extra)
        self.assertIn("--inspector-width", self.extra)
        self.assertIn("--gutter-width", self.extra)
        grid = " ".join(self.rules(self.extra, ".workspace"))
        self.assertIn("var(--sidebar-width) var(--gutter-width)", grid)

    def test_the_wide_design_keeps_the_columns_generous(self):
        import re

        root = re.search(r":root\s*\{([^}]*)\}", self.extra, flags=re.S).group(1)
        self.assertIn("--sidebar-width: 322px", root)
        self.assertIn("--inspector-width: 412px", root)

    def test_the_drag_lives_in_the_page_and_is_persisted(self):
        for marker in ("wireGutter", "pointerdown", "pointermove", "clampWidth",
                       "agetnic.layout.v1", "ROOM_FOR_CHAT", "applyLayout"):
            self.assertIn(marker, self.app_js, marker)
        self.assertIn("ArrowLeft", self.app_js)
        self.assertIn("dblclick", self.app_js)

    def test_sidebar_is_one_tidy_column(self):
        # diagnostics moved to the top bar: one button, no sidebar panel
        self.assertNotIn('id="btn-diag"', self.index)
        self.assertIn('id="btn-diag-top"', self.index)
        # one Runs panel with two tabs instead of two panels of prose
        self.assertIn('id="runs-tabs"', self.index)
        self.assertIn('data-runs="saved"', self.index)
        self.assertIn('data-runs="local"', self.index)
        # the paragraphs that made memory/runs wordy are gone
        for gone in (
            "Every conversation is stored in",
            "Click one to reopen it and keep chatting",
            "Every Auto run is written to",
            "Click a run to read its full trace",
            "Model ping + every search backend. Not run yet.",
        ):
            self.assertNotIn(gone, self.index, gone)
        # the diagnostics report moved into the inspector
        self.assertIn('id="diag-summary"', self.index)
        self.assertRegex(self.index, r"id=\"inspector\"[\s\S]{0,2000}id=\"diag-summary\"")

    def test_the_columns_scroll_inside_the_screen(self):
        # the studio shell is one fixed viewport: the page itself never scrolls,
        # each column scrolls inside itself (the "chat runs past the screen" bug)
        body = " ".join(self.rules(self.styles, "body"))
        self.assertIn("overflow: hidden", body)
        self.assertNotIn("height: calc(100vh", body)
        shell = " ".join(self.rules(self.styles, ".app-shell"))
        self.assertIn("flex-direction: column", shell)
        workspace = " ".join(self.rules(self.styles, ".workspace") + self.rules(self.extra, ".workspace"))
        self.assertIn("minmax(0, 1fr)", workspace)
        self.assertIn("overflow: hidden", workspace)
        self.assertNotIn("height: calc(100vh", workspace)
        # a tall inspector must not stretch the row
        for selector in (".chat-scroll", ".inspector", ".inspector-content"):
            body_rules = " ".join(self.rules(self.styles, selector) + self.rules(self.extra, selector))
            self.assertIn("min-height: 0", body_rules, selector)
        inspector = " ".join(self.rules(self.styles, ".inspector") + self.rules(self.extra, ".inspector"))
        self.assertIn("overflow: hidden", inspector)
        self.assertIn("overflow: auto", " ".join(self.rules(self.styles, ".inspector-content")))
        self.assertIn("overflow: auto", " ".join(self.rules(self.styles, ".chat-scroll")))
        # long JSON and the run list scroll, never push the layout
        json_view = " ".join(self.rules(self.extra, ".json-view"))
        self.assertIn("overflow-wrap: anywhere", json_view)
        self.assertIn("overflow: auto", " ".join(self.rules(self.styles, ".sidebar") + self.rules(self.extra, ".sidebar")))
        self.assertIn("display: grid", " ".join(self.rules(self.extra, ".run-list")))


if __name__ == "__main__":
    unittest.main()
