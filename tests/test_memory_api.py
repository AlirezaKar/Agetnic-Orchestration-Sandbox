"""HTTP surface of the harness memory: /sessions, and memory inside /orchestrate, /chat, /search.

The point of these tests is the *round trip*: a message is answered, the turn is
stored, the page can replay it, and the next message is answered with the
earlier conversation as context — without the browser resending anything.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings, get_settings
from app.llm.base import ModelResponse, ProviderError
from app.memory import SessionMemory
from app.search.base import SearchResponse, SearchResult


class RecordingModel:
    """Answers, and remembers every prompt it was given."""

    providers: list = []
    skipped: dict = {}

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append([dict(message) for message in messages])
        system = messages[0]["content"]
        if "manager of a small agent team" in system:
            text = f"echo[{len(self.calls)}] manager summary"
        elif "document analyst" in system:
            text = f"echo[{len(self.calls)}] document finding"
        elif "reviewing what came back" in system:
            text = '{"note": "nothing needed", "follow_up": []}'
        else:
            text = f"echo[{len(self.calls)}] agent answer"
        return ModelResponse(content=text, provider="fake", model="fake-1", usage={})

    def saw(self, needle: str) -> bool:
        return any(needle in json.dumps(call) for call in self.calls)


class FailingModel:
    providers: list = []
    skipped: dict = {}

    async def complete(self, messages, tools=None, **kwargs):
        raise ProviderError("fake", "upstream refused", status=500, hint="try later")


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


class MemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory_dir = Path(self.tmp.name) / "memory"
        self.model = RecordingModel()
        self.settings = Settings(
            _env_file=None,
            files_dir=str(Path(self.tmp.name) / "files"),
            memory_dir=str(self.memory_dir),
            orchestrator_timeout=5.0,
            orchestrator_planner="heuristic",
            orchestrator_review=False,
            orchestrator_log_runs=False,
        )
        get_settings.cache_clear()
        main_module.reset_store()
        main_module.reset_memory()
        self.patches = [
            mock.patch.object(main_module, "get_settings", lambda: self.settings),
            mock.patch.object(main_module, "build_chain", lambda settings=None: self.model),
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
        main_module.reset_memory()
        get_settings.cache_clear()
        self.tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def orchestrate(self, message: str, session_id: str | None = None, **extra):
        body = {"message": message, **extra}
        if session_id:
            body["session_id"] = session_id
        response = self.client.post("/orchestrate", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -------------------------------------------------------------- memory
    def test_a_turn_is_stored_and_the_session_id_comes_back(self):
        first = self.orchestrate("first question")
        session = first["session"]
        self.assertTrue(session["session_id"].startswith("s-"))
        self.assertEqual(session["turns"], 1)
        self.assertTrue(session["stored"])
        self.assertTrue(Path(session["path"]).is_file())
        self.assertTrue((self.memory_dir / "index.jsonl").is_file())

    def test_the_stored_turn_carries_what_the_page_rendered(self):
        first = self.orchestrate("first question")
        record = self.client.get(f"/sessions/{first['session']['session_id']}").json()
        self.assertEqual(len(record["turns"]), 1)
        turn = record["turns"][0]
        self.assertEqual(turn["user"]["text"], "first question")
        stored = turn["assistant"]["response"]
        self.assertEqual(stored["answer"], first["answer"])
        self.assertTrue(stored["orchestration"]["trace"], "the trace must replay too")
        self.assertTrue(stored["orchestration"]["plan"]["agents"])
        self.assertEqual(record["resume"]["turns"], 1)

    def test_the_list_and_the_detail_disagree_only_in_size(self):
        first = self.orchestrate("refund policy please")
        listing = self.client.get("/sessions").json()
        self.assertTrue(listing["enabled"])
        self.assertEqual(listing["count"], 1)
        item = listing["sessions"][0]
        self.assertEqual(item["session_id"], first["session"]["session_id"])
        self.assertEqual(item["title"], "refund policy please")
        self.assertEqual(item["turns"], 1)
        self.assertNotIn("turn_id", json.dumps(item), "the list must stay small")
        detail = self.client.get(f"/sessions/{item['session_id']}").json()
        self.assertIsInstance(detail["turns"], list)

    def test_the_next_message_gets_the_conversation_from_memory(self):
        first = self.orchestrate("first question")
        session_id = first["session"]["session_id"]
        earlier = first["answer"]
        self.assertTrue(earlier)

        second = self.orchestrate("second question", session_id=session_id)
        self.assertEqual(second["session"]["session_id"], session_id)
        self.assertEqual(second["session"]["turns"], 2)
        self.assertTrue(
            self.model.saw(earlier),
            "the second run must see the first answer — memory, not the browser, is the context",
        )
        # ...and the trace says so
        first_event = next(event for event in second["orchestration"]["trace"] if event["stage"] == "run")
        self.assertEqual(first_event["detail"]["history_turns"], 2)

    def test_the_browser_does_not_have_to_resend_the_history(self):
        first = self.orchestrate("first question")
        session_id = first["session"]["session_id"]
        earlier = first["answer"]
        self.orchestrate(
            "second question",
            session_id=session_id,
            history=[{"role": "user", "content": "BOGUS-CLIENT-HISTORY"}],
        )
        self.assertFalse(self.model.saw("BOGUS-CLIENT-HISTORY"))
        self.assertTrue(self.model.saw(earlier))

    def test_client_history_still_works_without_a_session(self):
        self.orchestrate(
            "carry this over", history=[{"role": "user", "content": "CLIENT-ONLY-CONTEXT"}]
        )
        self.assertTrue(self.model.saw("CLIENT-ONLY-CONTEXT"))

    def test_remember_false_keeps_the_turn_out_of_memory(self):
        first = self.orchestrate("do not remember me", remember=False)
        self.assertIsNone(first["session"])
        self.assertEqual(self.client.get("/sessions").json()["count"], 0)

    def test_a_failed_turn_is_remembered(self):
        with mock.patch.object(main_module, "build_chain", lambda settings=None: FailingModel()):
            response = self.client.post("/chat", json={"message": "will fail", "allow_search": False})
        self.assertEqual(response.status_code, 502, response.text)
        body = response.json()
        self.assertTrue(body["session"]["session_id"])
        record = self.client.get(f"/sessions/{body['session']['session_id']}").json()
        turn = record["turns"][0]
        self.assertFalse(turn["assistant"]["ok"])
        self.assertEqual(turn["assistant"]["error"]["status"], 502)
        self.assertIn("upstream refused", json.dumps(record))
        # a failed turn is not handed back to the model as if it were an answer
        memory = main_module.get_memory()
        self.assertEqual(len(memory.history(record["session_id"])), 1)

    def test_an_orchestrated_agent_failure_still_answers_and_is_stored(self):
        with mock.patch.object(main_module, "build_chain", lambda settings=None: FailingModel()):
            response = self.client.post("/orchestrate", json={"message": "agents will fail"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("No agent could answer", body["answer"])
        record = self.client.get(f"/sessions/{body['session']['session_id']}").json()
        self.assertTrue(record["turns"][0]["assistant"]["ok"])
        self.assertIn("upstream refused", json.dumps(record["turns"][0]["assistant"]["response"]))

    def test_a_deleted_session_is_not_resumed_by_accident(self):
        first = self.orchestrate("first question")
        session_id = first["session"]["session_id"]
        self.assertEqual(self.client.delete(f"/sessions/{session_id}").status_code, 200)
        second = self.orchestrate("after the delete", session_id=session_id)
        self.assertNotEqual(second["session"]["session_id"], session_id)

    # ---------------------------------------------------- chat and search
    def test_chat_turns_are_remembered_with_their_mode(self):
        response = self.client.post(
            "/chat", json={"message": "hello there", "allow_search": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["session"]["session_id"])
        record = self.client.get(f"/sessions/{body['session']['session_id']}").json()
        self.assertEqual(record["turns"][0]["mode"], "answer")
        self.assertEqual(record["turns"][0]["assistant"]["response"]["answer"], body["answer"])

    def test_chat_continues_from_memory(self):
        first = self.client.post("/chat", json={"message": "remember this", "allow_search": False}).json()
        second = self.client.post(
            "/chat",
            json={
                "message": "and now this",
                "allow_search": False,
                "session_id": first["session"]["session_id"],
            },
        ).json()
        self.assertEqual(second["session"]["turns"], 2)
        self.assertTrue(self.model.saw("echo[1]"))

    def test_search_turns_are_remembered_as_text(self):
        response = self.client.post("/search", json={"query": "python asyncio"})
        self.assertEqual(response.status_code, 200, response.text)
        record = self.client.get(f"/sessions/{response.json()['session']['session_id']}").json()
        turn = record["turns"][0]
        self.assertEqual(turn["mode"], "search")
        self.assertIn("Search backend: fake-search", turn["assistant"]["text"])

    # -------------------------------------------------------------- routes
    def test_create_rename_and_forget(self):
        created = self.client.post("/sessions", json={"title": "Fresh", "mode": "auto"})
        self.assertEqual(created.status_code, 200, created.text)
        session_id = created.json()["session_id"]
        self.assertEqual(created.json()["summary"]["turns"], 0)

        renamed = self.client.patch(f"/sessions/{session_id}", json={"title": "Renamed"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Renamed")

        self.assertEqual(self.client.delete(f"/sessions/{session_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/sessions/{session_id}").status_code, 404)
        self.assertEqual(self.client.delete(f"/sessions/{session_id}").status_code, 404)

    def test_unknown_session_is_a_helpful_404(self):
        response = self.client.get("/sessions/s-00000000-000000-000000")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"], "not_found")
        self.assertIn("hint", body)

    def test_routes_are_mirrored_under_api(self):
        self.orchestrate("hi")
        self.assertEqual(self.client.get("/api/sessions").json()["count"], 1)

    def test_health_and_version_report_memory(self):
        health = self.client.get("/health").json()
        self.assertTrue(health["memory"]["enabled"])
        self.assertEqual(health["memory"]["dir"], str(self.memory_dir))
        version = self.client.get("/version").json()
        self.assertEqual(version["memory"], str(self.memory_dir))
        self.assertEqual(version["sessions"], "/sessions")

    def test_memory_can_be_switched_off(self):
        self.settings.harness_memory = False
        main_module.reset_memory()
        body = self.orchestrate("no memory here")
        self.assertIsNone(body["session"])
        listing = self.client.get("/sessions").json()
        self.assertFalse(listing["enabled"])
        self.assertEqual(listing["count"], 0)
        self.assertEqual(self.client.post("/sessions", json={"title": "x"}).status_code, 503)

    def test_memory_survives_a_restart_of_the_store(self):
        first = self.orchestrate("first question")
        session_id = first["session"]["session_id"]
        main_module.reset_memory()  # a new process would do the same
        record = self.client.get(f"/sessions/{session_id}").json()
        self.assertEqual(record["summary"]["turns"], 1)
        self.assertTrue(record["turns"][0]["assistant"]["response"]["answer"])

    def test_titles_stay_stable_when_the_conversation_grows(self):
        first = self.orchestrate("What is the refund window?")
        self.orchestrate("and returns?", session_id=first["session"]["session_id"])
        item = self.client.get("/sessions").json()["sessions"][0]
        self.assertEqual(item["title"], "What is the refund window?")

    def test_store_and_memory_are_independent(self):
        # attachments live in .data/files, conversations in memory/: two bases
        store = main_module.get_memory()
        self.assertIsInstance(store, SessionMemory)
        self.assertEqual(Path(store.dir), self.memory_dir)


if "__main__" == __name__:  # pragma: no cover
    unittest.main()
