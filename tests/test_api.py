"""HTTP surface tests. No provider keys are set, so these also assert that the
app fails loudly (instead of silently answering from a mock)."""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import VERSION, app
import app.main as main_module


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The harness memory is real in this app: keep it out of the repository.
        cls._memory_tmp = tempfile.TemporaryDirectory()
        cls._memory_env = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = cls._memory_tmp.name
        get_settings.cache_clear()
        main_module.reset_memory()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        if cls._memory_env is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = cls._memory_env
        main_module.reset_memory()
        get_settings.cache_clear()
        cls._memory_tmp.cleanup()

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
            ("/assets/js/app.js", "runDiagnostics"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, response.text, path)

    def test_version_endpoint_matches_the_page(self):
        from pathlib import Path

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

    def test_health_reports_missing_model_and_usable_search(self):
        data = self.client.get("/health").json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["model_provider_ready"])
        self.assertIn("ddg", data["search_chain"])
        self.assertTrue(any("GROQ_API_KEY" in note for note in data["skipped"]["models"]))

    def test_config_hides_secrets(self):
        data = self.client.get("/config").json()
        flattened = str(data).lower()
        self.assertNotIn("api_key=", flattened)
        self.assertIn("search_chain", data)

    # ---------------------------------------------------------- providers
    def test_providers_lists_free_options_with_links(self):
        data = self.client.get("/providers").json()
        names = {p["name"] for p in data["providers"]}
        self.assertIn("groq", names)
        self.assertIn("gemini", names)
        self.assertIn("custom", names)
        self.assertNotIn("mock", names)
        for entry in data["providers"]:
            self.assertTrue(entry["free_tier"])
            self.assertTrue(entry["key_url"])
        search_names = {p["name"] for p in data["search"]["providers"]}
        self.assertTrue({"ddg", "wikipedia", "tavily", "serper", "google_cse"} <= search_names)

    def test_search_providers_flag_which_need_a_server(self):
        data = self.client.get("/providers").json()
        by_name = {p["name"]: p for p in data["search"]["providers"]}
        self.assertFalse(by_name["ddg"]["needs_server"])
        self.assertFalse(by_name["tavily"]["needs_server"])
        self.assertTrue(by_name["searxng"]["needs_server"])

    def test_models_endpoint_returns_configuration_error_without_key(self):
        response = self.client.get("/providers/gemini/models")
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "configuration")
        self.assertIn("GEMINI_API_KEY", str(body))

    def test_models_endpoint_rejects_unknown_provider(self):
        response = self.client.get("/providers/nope/models")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown provider", response.json()["message"])

    # ------------------------------------------------------------- search
    def test_search_rejects_unknown_provider(self):
        response = self.client.post("/search", json={"query": "x", "provider": "nope"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "search")

    def test_search_requires_query(self):
        response = self.client.post("/search", json={"query": ""})
        self.assertEqual(response.status_code, 422)

    def test_search_diagnose_skips_unkeyed_providers(self):
        data = self.client.get("/search/diagnose", params={"q": "x"}).json()
        skipped = {s["provider"]: s["reason"] for s in data["skipped"]}
        self.assertIn("wikipedia", data["summary"]["ok"] + data["summary"]["failed"] + list(skipped))
        self.assertNotIn("tavily", [c["provider"] for c in data["checked"]])

    # -------------------------------------------------------------- agent
    def test_chat_without_provider_returns_actionable_400(self):
        response = self.client.post("/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "configuration")
        self.assertIn("LLM_PROVIDER", body["message"])

    def test_chat_validates_message(self):
        response = self.client.post("/chat", json={"message": ""})
        self.assertEqual(response.status_code, 422)

    def test_api_prefix_mirrors_the_root_routes(self):
        root = self.client.get("/health").json()
        prefixed = self.client.get("/api/health").json()
        self.assertEqual(root["version"], prefixed["version"])
        self.assertEqual(root["search_chain"], prefixed["search_chain"])

    def test_api_prefix_chat_works_instead_of_404(self):
        response = self.client.post("/api/chat", json={"message": "hello"})
        # no key configured -> the same actionable 400 as the unprefixed route
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "configuration")

    def test_api_root_lists_routes(self):
        data = self.client.get("/api").json()
        self.assertIn("version", data)
        self.assertTrue(any("/chat" in route for route in data["routes"]))

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

    def test_diagnose_returns_hints(self):
        data = self.client.get("/diagnose").json()
        self.assertIn("hints", data)
        self.assertFalse(data["model"]["ok"])
        self.assertTrue(any("LLM_PROVIDER" in hint for hint in data["hints"]))


if __name__ == "__main__":
    unittest.main()


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
        self.assertIn('src="assets/js/app.js', self.index)
        self.assertNotIn('href="/assets/', self.index)
        self.assertNotIn('src="/assets/', self.index)

    def test_asset_urls_are_cache_busted_with_the_app_version(self):
        self.assertIn(f"styles.css?v={VERSION}", self.index)
        self.assertIn(f"extra.css?v={VERSION}", self.index)
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

