"""HTTP surface of the manager: /orchestrate, /files, /agents."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.llm.base import ModelResponse
from app.search.base import SearchResponse, SearchResult


class FakeModel:
    providers: list = []  # /health introspects the chain
    skipped: dict = {}

    async def complete(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        if "manager of a small agent team" in system:
            text = "Manager summary."
        elif "document analyst" in system:
            text = "The policy allows refunds in 30 days."
        else:
            text = "General answer."
        return ModelResponse(content=text, provider="fake", model="fake-1", usage={})


class FakeSearchChain:
    providers: list = []
    skipped: dict = {}

    async def search(self, query, limit=5):
        return SearchResponse(
            query=query,
            provider="fake-search",
            results=[SearchResult(title="R", url="https://example.org", snippet="s")],
            latency_ms=2,
        )


class OrchestratorApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            files_dir=str(Path(self.tmp.name) / "files"),
            orchestrator_timeout=5.0,
        )
        get_settings.cache_clear()
        main_module.reset_store()
        self.patches = [
            mock.patch.object(main_module, "get_settings", lambda: self.settings),
            mock.patch.object(main_module, "build_chain", lambda settings=None: FakeModel()),
            mock.patch.object(
                main_module, "build_search_chain", lambda settings=None: FakeSearchChain()
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(main_module.app)

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        main_module.reset_store()
        get_settings.cache_clear()
        self.tmp.cleanup()

    # --------------------------------------------------------------- files
    def test_upload_text_file(self):
        response = self.client.post(
            "/files", json={"name": "policy.md", "text": "Refund window is 30 days."}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["name"], "policy.md")
        self.assertEqual(body["kind"], "text")
        self.assertTrue(body["id"])
        self.assertIn("Refund", body["preview"])

    def test_upload_base64_csv(self):
        encoded = base64.b64encode(b"month,revenue\nJan,10\n").decode()
        response = self.client.post("/files", json={"name": "sales.csv", "data_b64": encoded})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["columns"], ["month", "revenue"])
        self.assertEqual(response.json()["rows"], 1)

    def test_upload_rejects_binary(self):
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(256))).decode()
        response = self.client.post("/files", json={"name": "x.png", "data_b64": encoded})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["error"], "file")
        self.assertIn("hint", response.json())

    def test_upload_rejects_legacy_doc(self):
        encoded = base64.b64encode(b"\xd0\xcf\x11\xe0old word").decode()
        response = self.client.post("/files", json={"name": "old.doc", "data_b64": encoded})
        self.assertEqual(response.status_code, 415)
        self.assertIn(".docx", response.json()["hint"])

    def test_upload_requires_content(self):
        response = self.client.post("/files", json={"name": "nothing.txt"})
        self.assertEqual(response.status_code, 400)

    def test_list_and_delete(self):
        upload = self.client.post("/files", json={"name": "a.txt", "text": "alpha"}).json()
        listing = self.client.get("/files").json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["files"][0]["id"], upload["id"])
        self.assertEqual(self.client.delete(f"/files/{upload['id']}").status_code, 200)
        self.assertEqual(self.client.get("/files").json()["count"], 0)
        self.assertEqual(self.client.delete(f"/files/{upload['id']}").status_code, 404)

    # ----------------------------------------------------------- /agents
    def test_agents_endpoint_lists_the_roster(self):
        body = self.client.get("/agents").json()
        names = [agent["name"] for agent in body["agents"]]
        self.assertEqual(names, ["document", "search", "data", "general"])
        self.assertIn("roster", body)
        self.assertEqual(body["planner"], "heuristic")
        self.assertTrue(body["enabled"])

    # ------------------------------------------------------- /orchestrate
    def test_orchestrate_runs_agents_and_returns_a_plan(self):
        upload = self.client.post(
            "/files", json={"name": "policy.md", "text": "Refunds are allowed within 30 days."}
        ).json()
        response = self.client.post(
            "/orchestrate",
            json={
                "message": "Summarise the refund policy in the attached file.",
                "file_ids": [upload["id"]],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["mode"], "auto")
        orch = body["orchestration"]
        self.assertEqual(orch["plan"]["agents"], ["document"])
        self.assertEqual(orch["agents"][0]["agent"], "document")
        self.assertEqual(orch["agents"][0]["status"], "ok")
        self.assertTrue(orch["synthesized"])
        self.assertEqual(body["answer"], "Manager summary.")
        self.assertEqual(orch["files"][0]["name"], "policy.md")

    def test_orchestrate_without_files_uses_general_agent(self):
        body = self.client.post(
            "/orchestrate", json={"message": "Explain how TCP handshakes work."}
        ).json()
        self.assertEqual(body["orchestration"]["plan"]["agents"], ["general"])

    def test_orchestrate_unknown_file_id(self):
        response = self.client.post(
            "/orchestrate", json={"message": "summarise it", "file_ids": ["deadbeef0000"]}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "file_not_found")

    def test_orchestrate_rejects_empty_message(self):
        response = self.client.post("/orchestrate", json={"message": ""})
        self.assertEqual(response.status_code, 422)

    def test_orchestrate_rejects_unknown_planner(self):
        response = self.client.post(
            "/orchestrate", json={"message": "hi", "planner": "magic"}
        )
        self.assertEqual(response.status_code, 422)

    def test_orchestrate_respects_agent_override(self):
        body = self.client.post(
            "/orchestrate",
            json={"message": "What changed in the latest Python release?", "agents": ["general"]},
        ).json()
        self.assertEqual(body["orchestration"]["plan"]["agents"], ["general"])

    def test_orchestrate_can_disable_synthesis(self):
        body = self.client.post(
            "/orchestrate",
            json={"message": "Explain closures.", "synthesize": False},
        ).json()
        self.assertFalse(body["orchestration"]["synthesized"])

    def test_orchestrate_503_when_disabled(self):
        self.settings.orchestrator_enabled = False
        response = self.client.post("/orchestrate", json={"message": "hi"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "disabled")

    def test_orchestrate_502_on_provider_error(self):
        class Broken:
            async def complete(self, *args, **kwargs):
                raise RuntimeError("provider down")

        with mock.patch.object(main_module, "build_chain", lambda settings=None: Broken()):
            response = self.client.post("/orchestrate", json={"message": "Explain closures."})
        self.assertEqual(response.status_code, 200)  # agents degrade, the run survives
        self.assertTrue(response.json()["orchestration"]["agents"])

    def test_health_reports_orchestrator(self):
        body = self.client.get("/health").json()
        self.assertTrue(body["orchestrator"]["enabled"])
        self.assertEqual(body["orchestrator"]["planner"], "heuristic")
        self.assertIn("files", body)

    def test_api_prefix_aliases(self):
        self.assertEqual(self.client.get("/api/agents").status_code, 200)
        self.assertEqual(self.client.get("/api/files").status_code, 200)


if __name__ == "__main__":
    unittest.main()
