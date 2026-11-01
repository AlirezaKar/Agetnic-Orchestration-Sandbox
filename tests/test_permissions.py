"""v0.11.0 regressions: a run record that always serialises, and file permission.

Two failures from the Windows log:

* ``POST /orchestrate`` → 500 ``TypeError: Object of type PlanStep is not JSON
  serializable`` — the manager's review kept live ``PlanStep`` objects in the
  record, and ``runlog.save_run`` dumped it with no fallback.
* ``GET /favicon.ico`` → 500 ``Response content longer than Content-Length``.

And the permission prompt: a file is only handed to an agent after the user
allows it (``FILES_PERMISSION_PROMPT``, off by default in this trusted
workspace).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.llm.base import ModelResponse
from app.orchestrator import OrchestrationRequest, OrchestratorService, runlog
from app.orchestrator.files import Attachment, AttachmentStore
from app.orchestrator.planner import PlanStep

FOLLOW_UP = json.dumps(
    {
        "note": "the total is missing",
        "follow_up": [{"agent": "data", "task": "add the figures", "reason": "needs a total"}],
    }
)


class ReviewModel:
    """Answers the manager's review with a follow-up step, then the synthesis."""

    providers: list = []
    skipped: dict = {}

    async def complete(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        if "reviewing what came back" in system:
            return ModelResponse(content=FOLLOW_UP, provider="fake", model="fake-1", usage={})
        if "manager of a small agent team" in system:
            return ModelResponse(content="Manager summary.", provider="fake", model="fake-1", usage={})
        return ModelResponse(content="An agent answer.", provider="fake", model="fake-1", usage={})


class RunRecordTests(unittest.TestCase):
    """The exact 500: a review with follow-up steps + run logging on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            orchestrator_planner="heuristic",
            orchestrator_review=True,
            orchestrator_log_runs=True,
            files_dir=str(Path(self.tmp.name) / "files"),
            memory_dir=str(Path(self.tmp.name) / "memory"),
        )
        self.service = OrchestratorService(
            self.settings, models=ReviewModel(), search=None, log_dir=self.tmp.name
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, message: str):
        return asyncio.run(self.service.run(OrchestrationRequest(message=message)))

    def test_review_steps_are_plain_in_the_record(self):
        result = self._run("What is the latest Python release, and why does it matter?")
        self.assertTrue(result.review.get("steps"), "the review should have produced a step")
        record = result.record()
        json.dumps(record)  # used to raise TypeError: PlanStep is not JSON serializable
        steps = record["orchestration"]["review"]["steps"]
        self.assertIsInstance(steps[0], dict)
        self.assertEqual(steps[0]["agent"], "data")

    def test_the_run_log_is_written_for_a_reviewed_run(self):
        result = self._run("What is the latest Python release, and why does it matter?")
        self.assertTrue(result.log_path, "the run log should exist")
        written = json.loads(Path(result.log_path).read_text(encoding="utf-8"))
        self.assertEqual(written["run_id"], result.run_id)
        self.assertEqual(written["orchestration"]["review"]["steps"][0]["agent"], "data")

    def test_the_http_payload_of_the_same_run_is_serialisable(self):
        result = self._run("What is the latest Python release, and why does it matter?")
        payload = self.service.payload(result)
        json.dumps(payload)
        self.assertIsInstance(payload["orchestration"]["review"]["steps"][0], dict)


class ToPlainTests(unittest.TestCase):
    def test_dataclasses_and_to_dict_objects_become_plain(self):
        step = PlanStep(agent="data", task="add", reason="because")
        plain = runlog.to_plain({"steps": [step], "nested": {"ok": 1}, "t": (1, 2)})
        self.assertEqual(plain["steps"][0]["task"], "add")
        self.assertEqual(plain["nested"], {"ok": 1})
        self.assertEqual(plain["t"], [1, 2])

    def test_unknown_objects_are_stringified_not_raised(self):
        self.assertEqual(runlog.to_plain(object()).startswith("<object object at"), True)

    def test_save_run_never_raises_on_a_bad_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {"run_id": "run-bad", "orchestration": {"review": {"steps": [object()]}}}
            path = runlog.save_run(record, log_dir=tmp)
            self.assertIsNotNone(path)
            written = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertIn("steps", written["orchestration"]["review"])


class FaviconTests(unittest.TestCase):
    def test_favicon_answers_204_without_a_body(self):
        client = TestClient(main_module.app)
        response = client.get("/favicon.ico")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")


class FilePermissionApiTests(unittest.TestCase):
    """Upload → prompt → allow / refuse → the agent may (or may not) read it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = Settings(
            _env_file=None,
            files_dir=str(self.root / "files"),
            memory_dir=str(self.root / "memory"),
            orchestrator_planner="heuristic",
            orchestrator_review=False,
            orchestrator_log_runs=False,
            files_permission_prompt=True,
        )
        get_settings.cache_clear()
        main_module.reset_store()
        main_module.reset_memory()
        self.patches = [
            mock.patch.object(main_module, "get_settings", lambda: self.settings),
            mock.patch.object(main_module, "build_chain", lambda settings=None: ReviewModel()),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self._cleanup)
        self.client = TestClient(main_module.app)

    def _cleanup(self):
        main_module.reset_store()
        main_module.reset_memory()
        get_settings.cache_clear()
        self.tmp.cleanup()

    # --------------------------------------------------------- with the flag
    def test_upload_without_an_answer_waits_for_permission(self):
        body = self.client.post("/files", json={"name": "policy.md", "text": "Refunds: 30 days."}).json()
        self.assertFalse(body["consent"])
        self.assertTrue(body["needs_consent"])
        self.assertTrue(body["stored"])

    def test_the_agent_cannot_read_a_file_nobody_allowed(self):
        file_id = self.client.post(
            "/files", json={"name": "policy.md", "text": "Refunds: 30 days."}
        ).json()["id"]
        response = self.client.post(
            "/orchestrate", json={"message": "What is the refund window?", "file_ids": [file_id]}
        )
        self.assertEqual(response.status_code, 403, response.text)
        body = response.json()
        self.assertEqual(body["error"], "consent_required")
        self.assertEqual(body["files"][0]["id"], file_id)
        self.assertIn("hint", body)

    def test_allowing_the_file_lets_the_run_through(self):
        file_id = self.client.post(
            "/files", json={"name": "policy.md", "text": "Refunds: 30 days."}
        ).json()["id"]
        granted = self.client.post(f"/files/{file_id}/consent", json={"granted": True})
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assertTrue(granted.json()["consent"])
        self.assertTrue(granted.json()["allowed"])
        response = self.client.post(
            "/orchestrate", json={"message": "What is the refund window?", "file_ids": [file_id]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["orchestration"]["files"][0]["id"], file_id)

    def test_refusing_deletes_the_file(self):
        file_id = self.client.post("/files", json={"name": "secret.txt", "text": "no"}).json()["id"]
        refused = self.client.post(f"/files/{file_id}/consent", json={"granted": False})
        self.assertEqual(refused.status_code, 200)
        self.assertTrue(refused.json()["deleted"])
        self.assertEqual(self.client.get("/files").json()["count"], 0)

    def test_consent_is_persisted_in_the_store(self):
        file_id = self.client.post("/files", json={"name": "a.txt", "text": "alpha"}).json()["id"]
        self.client.post(f"/files/{file_id}/consent", json={"granted": True})
        main_module.reset_store()  # fresh store, read back from disk
        reloaded = main_module.get_store().get(file_id)
        self.assertIsNotNone(reloaded)
        self.assertTrue(reloaded.consent)

    def test_unknown_file_answers_404(self):
        self.assertEqual(
            self.client.post("/files/nope/consent", json={"granted": True}).status_code, 404
        )

    # ------------------------------------------------------ flag off (default)
    def test_without_the_prompt_a_file_is_allowed_at_once(self):
        self.settings.files_permission_prompt = False
        body = self.client.post("/files", json={"name": "a.txt", "text": "alpha"}).json()
        self.assertTrue(body["consent"])
        self.assertNotIn("needs_consent", body)

    def test_an_explicit_refusal_still_blocks_the_agent(self):
        body = self.client.post(
            "/files", json={"name": "a.txt", "text": "alpha", "consent": False}
        ).json()
        self.assertFalse(body["consent"])
        response = self.client.post(
            "/orchestrate", json={"message": "read it", "file_ids": [body["id"]]}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "consent_required")

    # ----------------------------------------------------------- observability
    def test_health_and_config_report_the_switch(self):
        health = self.client.get("/health").json()
        self.assertTrue(health["files"]["permission_prompt"])
        self.assertIn("pending", health["files"])
        self.assertIn("writable", health["files"])
        self.assertTrue(self.client.get("/config").json()["files_permission_prompt"])

    def test_listing_shows_the_consent_state(self):
        self.client.post("/files", json={"name": "a.txt", "text": "alpha"})
        listing = self.client.get("/files").json()
        self.assertEqual(listing["pending"], 1)
        self.assertFalse(listing["files"][0]["consent"])
        self.assertTrue(listing["permission_prompt"])


class StorePermissionTests(unittest.TestCase):
    """A read-only FILES_DIR must not look like a mysterious permission error."""

    def test_an_unwritable_store_still_keeps_the_file_in_memory_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(tmp)
            attachment = Attachment(id="abc", name="a.txt", suffix=".txt", size=5, text="alpha")
            with mock.patch.object(Path, "write_text", side_effect=PermissionError("denied")):
                store.save(attachment)
            self.assertFalse(store.writable)
            self.assertIn("PermissionError", store.last_error)
            self.assertIsNotNone(store.get("abc"))

    def test_consent_round_trips_through_the_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AttachmentStore(tmp)
            store.save(Attachment(id="abc", name="a.txt", suffix=".txt", size=5, text="alpha"))
            self.assertTrue(store.get("abc").consent)
            store.set_consent("abc", False)
            again = AttachmentStore(tmp).get("abc")
            self.assertFalse(again.consent)
            self.assertEqual([item.id for item in store.pending()], ["abc"])


if __name__ == "__main__":
    unittest.main()
