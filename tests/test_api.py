"""HTTP surface tests. No provider keys are set, so these also assert that the
app fails loudly (instead of silently answering from a mock)."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        get_settings.cache_clear()
        cls.client = TestClient(app)

    # ------------------------------------------------------------- basics
    def test_index_serves_test_harness(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Agetnic Orchestration Sandbox", response.text)
        self.assertIn("Run diagnostics", response.text)
        self.assertIn("/assets/js/app.js", response.text)
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

    def test_diagnose_returns_hints(self):
        data = self.client.get("/diagnose").json()
        self.assertIn("hints", data)
        self.assertFalse(data["model"]["ok"])
        self.assertTrue(any("LLM_PROVIDER" in hint for hint in data["hints"]))


if __name__ == "__main__":
    unittest.main()
