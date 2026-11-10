"""HTTP surface of the manager: /orchestrate, /files, /agents."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main_module
from app.aras import ModelResponse, SearchResponse, SearchResult, ToolCall
from app.config import Settings, get_settings


class FakeModel:
    async def complete(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        if "You are the evaluator" in system:
            return ModelResponse(
                content=(
                    '{"summary": "one report needed tightening", "gaps": ["no date"], '
                    '"reports": [{"agent": "document", "verdict": "revise", "score": 4, '
                    '"issues": ["buries the answer"], '
                    '"rewrite": "Refunds are allowed within 30 days."}]}'
                ),
                provider="fake",
                model="fake-1",
                usage={},
            )
        if "manager of a small agent team" in system:
            text = "Manager summary."
        elif "document analyst" in system:
            text = "The policy allows refunds in 30 days."
        else:
            text = "General answer."
        return ModelResponse(content=text, provider="fake", model="fake-1", usage={})


class FakeSearchChain:
    async def search(self, query, limit=5):
        return SearchResponse(
            query=query,
            provider="fake-search",
            results=[SearchResult(title="R", url="https://example.org", snippet="s")],
            latency_ms=2,
        )


class _ApiCase(unittest.TestCase):
    """Shared harness: a temp file store, in-memory sessions, fake model/search."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            files_dir=str(Path(self.tmp.name) / "files"),
            memory_dir=str(Path(self.tmp.name) / "memory"),
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
            mock.patch.object(main_module, "build_model", lambda settings=None: FakeModel()),
            mock.patch.object(
                main_module, "build_search", lambda settings=None: FakeSearchChain()
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


class OrchestratorApiTests(_ApiCase):
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
        self.assertIn("review", body)
        self.assertTrue(body["enabled"])
        # the evaluator gate is advertised too
        self.assertTrue(body["evaluate"])
        self.assertTrue(body["optimize"])
        self.assertEqual(body["review_rounds"], 1)

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

    def test_orchestrate_evaluates_before_the_manager_reads(self):
        upload = self.client.post(
            "/files", json={"name": "policy.md", "text": "Refunds are allowed within 30 days."}
        ).json()
        body = self.client.post(
            "/orchestrate",
            json={
                "message": "Summarise the refund policy in the attached file.",
                "file_ids": [upload["id"]],
            },
        ).json()
        evaluation = body["orchestration"]["evaluation"]
        self.assertTrue(evaluation["requested"])
        self.assertTrue(evaluation["ran"])
        self.assertEqual(evaluation["method"], "model")
        self.assertEqual(evaluation["quality"], 4.0)
        self.assertEqual(evaluation["revised"], 1)
        self.assertEqual(evaluation["gaps"], ["no date"])
        agent = body["orchestration"]["agents"][0]
        self.assertTrue(agent["revised"])
        self.assertEqual(agent["optimized"], "Refunds are allowed within 30 days.")
        stages = [event["stage"] for event in body["orchestration"]["trace"]]
        self.assertIn("evaluate", stages)
        # the gate sits between the agents and the synthesis
        self.assertLess(stages.index("agent"), stages.index("evaluate"))
        self.assertLess(stages.index("evaluate"), stages.index("synthesis"))

    def test_orchestrate_can_switch_the_evaluator_off(self):
        body = self.client.post(
            "/orchestrate",
            json={"message": "Explain closures.", "evaluate": False},
        ).json()
        evaluation = body["orchestration"]["evaluation"]
        self.assertFalse(evaluation["requested"])
        self.assertFalse(evaluation["ran"])
        self.assertIn("disabled", evaluation["reason"])
        self.assertEqual(evaluation["items"], [])

    def test_orchestrate_503_when_disabled(self):
        self.settings.orchestrator_enabled = False
        response = self.client.post("/orchestrate", json={"message": "hi"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "disabled")

    def test_orchestrate_502_on_provider_error(self):
        class Broken:
            async def complete(self, *args, **kwargs):
                raise RuntimeError("provider down")

        with mock.patch.object(main_module, "build_model", lambda settings=None: Broken()):
            response = self.client.post("/orchestrate", json={"message": "Explain closures."})
        self.assertEqual(response.status_code, 200)  # agents degrade, the run survives
        self.assertTrue(response.json()["orchestration"]["agents"])

    def test_health_reports_orchestrator(self):
        body = self.client.get("/health").json()
        self.assertTrue(body["orchestrator"]["enabled"])
        self.assertEqual(body["orchestrator"]["planner"], "heuristic")
        self.assertIn("review", body["orchestrator"])
        self.assertIn("files", body)


    # ------------------------------------------------------- trace / logs
    def test_orchestrate_returns_a_step_by_step_trace(self):
        upload = self.client.post(
            "/files", json={"name": "policy.md", "text": "Refunds within 30 days."}
        ).json()
        payload = self.client.post(
            "/orchestrate",
            json={
                "message": "Summarise the refund policy and compute the average of 10, 20, 40.",
                "file_ids": [upload["id"]],
            },
        ).json()
        orch = payload["orchestration"]
        self.assertTrue(orch["run_id"].startswith("run-"))
        self.assertTrue(orch["trace"])
        titles = [event["title"] for event in orch["trace"]]
        self.assertIn("Request received", titles)
        self.assertIn("Final synthesis prompt", titles)
        self.assertIn("Final answer", titles)
        # the planner prompt and each agent's prompt are in the trace, with messages
        planner = [e for e in orch["trace"] if e["title"].startswith("Selected the agents")]
        self.assertTrue(planner)
        agent_prompts = [
            e for e in orch["trace"] if e["actor"] in ("document", "data") and e["detail"].get("messages")
        ]
        self.assertTrue(agent_prompts)
        self.assertIn("role", agent_prompts[0]["detail"]["messages"][0])
        # waves describe the parallelism
        self.assertTrue(orch["waves"])
        self.assertEqual(orch["waves"][0]["wave"], 1)

    def test_detail_trace_is_clipped(self):
        self.settings.orchestrator_trace_chars = 40
        payload = self.client.post("/orchestrate", json={"message": "Explain closures."}).json()
        prompts = [
            event for event in payload["orchestration"]["trace"] if event["detail"].get("messages")
        ]
        self.assertTrue(prompts)
        longest = max(
            (message["content"] for event in prompts for message in event["detail"]["messages"]),
            key=len,
        )
        self.assertIn("more chars", longest)

    def test_runs_endpoint_lists_and_returns_a_run(self):
        with mock.patch.dict("os.environ", {"LOG_DIR": str(Path(self.tmp.name) / "logs")}):
            self.settings.orchestrator_log_runs = True
            payload = self.client.post(
                "/orchestrate", json={"message": "Explain closures in JavaScript."}
            ).json()
            run_id = payload["orchestration"]["run_id"]
            listing = self.client.get("/runs").json()
            self.assertEqual(listing["count"], 1)
            self.assertEqual(listing["runs"][0]["run_id"], run_id)
            detail = self.client.get(f"/runs/{run_id}").json()
            self.assertEqual(detail["run_id"], run_id)
            self.assertTrue(detail["orchestration"]["trace"])
            missing = self.client.get("/runs/run-does-not-exist")
            self.assertEqual(missing.status_code, 404)
            self.assertIn("hint", missing.json())

    def test_runs_are_empty_when_logging_is_off(self):
        with mock.patch.dict("os.environ", {"LOG_DIR": str(Path(self.tmp.name) / "logs2")}):
            self.client.post("/orchestrate", json={"message": "Explain closures."})
            self.assertEqual(self.client.get("/runs").json()["count"], 0)

    def test_planner_can_be_overridden_per_request(self):
        response = self.client.post(
            "/orchestrate",
            json={"message": "Explain closures.", "planner": "auto"},
        )
        self.assertEqual(response.status_code, 200)
        plan = response.json()["orchestration"]["plan"]
        # this fake model returns prose, not a plan, so "auto" falls back to the rules
        self.assertEqual(plan["strategy"], "heuristic")
        self.assertEqual(plan["detail_source"], "rules")
        self.assertEqual(self.client.post("/orchestrate", json={"message": "x", "planner": "magic"}).status_code, 422)

    def test_review_flag_can_be_turned_off_per_request(self):
        payload = self.client.post(
            "/orchestrate",
            json={
                "message": "Summarise this and compute the total of 10, 20, 30.",
                "review": False,
            },
        ).json()
        self.assertFalse(payload["orchestration"]["review"]["asked"])

    def test_api_prefix_aliases(self):
        self.assertEqual(self.client.get("/api/agents").status_code, 200)
        self.assertEqual(self.client.get("/api/files").status_code, 200)


if __name__ == "__main__":
    unittest.main()


class OrchestrateStreamTests(_ApiCase):
    """``POST /orchestrate/stream``: the same turn, watched while it happens."""

    def frames(self, payload: dict) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        kind = ""
        with self.client.stream("POST", "/orchestrate/stream", json=payload) as response:
            self.assertEqual(response.status_code, 200, response.read())
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
            for line in response.iter_lines():
                if line.startswith("event: "):
                    kind = line[len("event: "):]
                elif line.startswith("data: "):
                    out.append((kind, json.loads(line[len("data: "):])))
        return out

    # ------------------------------------------------------------- framing
    def test_the_stream_opens_with_the_run_id(self):
        frames = self.frames({"message": "Explain how TCP handshakes work."})
        self.assertEqual(frames[0][0], "open")
        self.assertTrue(frames[0][1]["run_id"].startswith("run-"))
        self.assertEqual(frames[-1][0], "result")

    def test_thoughts_follow_the_pipeline(self):
        frames = self.frames({"message": "What changed in the latest Python release? Cite your sources."})
        thoughts = [body for kind, body in frames if kind == "thought"]
        self.assertGreater(len(thoughts), 5)
        categories = [item["category"] for item in thoughts]
        # planner → agents → evaluator → manager, in that order
        self.assertEqual(categories[0], "manager")  # "Request received"
        self.assertIn("planner", categories)
        self.assertIn("agent", categories)
        self.assertIn("evaluator", categories)
        self.assertLess(categories.index("planner"), categories.index("agent"))
        self.assertLess(categories.index("agent"), categories.index("evaluator"))
        self.assertGreater(categories.index("evaluator"), 0)
        # every thought carries what the page needs to group and draw it
        for item in thoughts:
            for key in ("seq", "category", "group", "label", "kind", "title", "text", "offset_ms"):
                self.assertIn(key, item)
            self.assertNotIn("messages", item["detail"])
        # the final answer is never one of the thoughts
        self.assertFalse(any(item["kind"] == "reply" and "Manager summary" in item["text"] for item in thoughts))

    def test_the_agent_thoughts_show_the_work(self):
        frames = self.frames({"message": "What changed in the latest Python release? Cite your sources."})
        thoughts = [body for kind, body in frames if kind == "thought"]
        search = [item for item in thoughts if item["group"] == "agent:search"]
        self.assertTrue(search, "the search agent should have its own group")
        kinds = {item["kind"] for item in search}
        self.assertIn("note", kinds)     # the task it was given
        self.assertIn("prompt", kinds)   # what it sent to the model
        self.assertIn("reply", kinds)    # what came back
        self.assertEqual({item["label"] for item in search}, {"Web researcher"})

    def test_the_last_frame_is_the_same_payload_as_the_plain_route(self):
        frames = self.frames({"message": "Explain closures in JavaScript."})
        kind, body = frames[-1]
        self.assertEqual(kind, "result")
        for key in ("answer", "mode", "run_id", "orchestration", "session"):
            self.assertIn(key, body)
        self.assertEqual(body["mode"], "auto")
        orchestration = body["orchestration"]
        self.assertIn("evaluation", orchestration)
        self.assertTrue(orchestration["agents"])
        self.assertTrue(body["session"]["session_id"])

    def test_a_streamed_run_is_remembered_like_any_other(self):
        frames = self.frames({"message": "Explain closures in JavaScript."})
        session_id = frames[-1][1]["session"]["session_id"]
        turns = self.client.get(f"/sessions/{session_id}").json()
        self.assertEqual(len(turns["turns"]), 1)
        self.assertEqual(turns["turns"][0]["user"]["text"], "Explain closures in JavaScript.")

    def test_validation_errors_stay_plain_json(self):
        response = self.client.post(
            "/orchestrate/stream", json={"message": "summarise it", "file_ids": ["deadbeef0000"]}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "file_not_found")
        self.assertTrue(response.headers["content-type"].startswith("application/json"))

    def test_a_disabled_orchestrator_never_opens_a_stream(self):
        self.settings.orchestrator_enabled = False
        response = self.client.post("/orchestrate/stream", json={"message": "hi"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "disabled")

    def test_a_failing_run_ends_with_an_error_frame_not_a_hang(self):
        class Broken:
            providers = []
            skipped = {}

            async def complete(self, *args, **kwargs):
                raise RuntimeError("provider down")

        with mock.patch.object(main_module, "build_model", lambda settings=None: Broken()):
            frames = self.frames({"message": "Explain closures."})
        # the agents degrade rather than fail the run, so this is still a result
        self.assertEqual(frames[-1][0], "result")
        self.assertTrue(frames[-1][1]["orchestration"]["agents"])

    def test_a_reader_that_leaves_lets_the_run_finish(self):
        """The work is already paid for: closing the tab must not throw it away."""
        import asyncio

        from app.orchestrator.runbus import BUS

        async def scenario():
            payload = main_module.OrchestrateRequest(message="Explain closures in JavaScript.")
            sessions = main_module.get_memory()
            channel = BUS.open("run-detach")
            stream = main_module._stream_run(
                self.settings, payload, [], [], sessions, "run-detach", channel, channel.publish
            )
            opening = await stream.__anext__()
            self.assertIn("event: open", opening)
            await stream.aclose()  # the browser went away
            self.assertIsNone(BUS.get("run-detach"))
            for _ in range(100):  # the run is detached, not cancelled
                if sessions.list():
                    break
                await asyncio.sleep(0.02)
            return sessions.list()

        listed = asyncio.run(scenario())
        self.assertTrue(listed, "the turn should still be remembered")
        self.assertEqual(listed[0]["turns"], 1)
        turns = self.client.get(f"/sessions/{listed[0]['session_id']}").json()
        self.assertTrue(turns["turns"][0]["assistant"]["ok"])

    # The live bug, end to end: an agent that really calls web_search, so the
    # trace records ``tool_calls: 1`` (a count, not a list) on its last event.
    # v0.15.0 answered that with a 500 in the middle of the run.
    def test_a_run_that_really_uses_a_tool_streams_to_the_answer(self):
        class ToolModel:
            async def complete(self, messages, tools=None, **kwargs):
                used = any(message.get("role") == "tool" for message in messages)
                if tools and not used:
                    return ModelResponse(
                        content="",
                        provider="fake",
                        model="fake-1",
                        usage={},
                        tool_calls=[ToolCall(name="web_search", arguments={"query": "latest release"})],
                    )
                return ModelResponse(
                    content="Python 3.14 is the latest (https://python.org/downloads).",
                    provider="fake",
                    model="fake-1",
                    usage={},
                )

        with mock.patch.object(main_module, "build_model", lambda settings=None: ToolModel()):
            frames = self.frames({"message": "What is the latest Python release? Cite sources."})

        self.assertEqual(frames[-1][0], "result")
        body = frames[-1][1]
        self.assertTrue(body["answer"].startswith("Python 3.14"))
        self.assertEqual(body["orchestration"]["agents"][0]["tool_calls"], 1)

        thoughts = [item for kind, item in frames if kind == "thought"]
        search = [item for item in thoughts if item["group"] == "agent:search"]
        self.assertTrue(any(item["kind"] == "tool" for item in search), "the tool call should be a thought")
        finished = [item for item in search if "finished" in item["title"].lower()]
        self.assertTrue(finished, "the agent should report how it ended")
        # the count must not be mistaken for a list of calls
        self.assertNotIn("tools", finished[0]["detail"])
        self.assertEqual(finished[0]["detail"]["status"], "ok")

    def test_a_projection_that_raises_costs_one_thought_not_the_run(self):
        """A thought is decoration; the answer must still arrive."""
        real = main_module.live_thought
        calls = {"n": 0}

        def flaky(event):
            calls["n"] += 1
            if calls["n"] == 3:  # exactly one event explodes
                raise TypeError("'int' object is not iterable")
            return real(event)

        with mock.patch.object(main_module, "live_thought", flaky):
            frames = self.frames({"message": "Explain closures in JavaScript."})
        self.assertGreater(calls["n"], 3)
        self.assertEqual(frames[-1][0], "result")
        self.assertTrue(frames[-1][1]["answer"])
        thoughts = [body for kind, body in frames if kind == "thought"]
        self.assertEqual(len(thoughts), calls["n"])  # the failed one became a plain line
        plain = [item for item in thoughts if item["detail"] == {}]
        self.assertTrue(plain, "the fallback thought should be in the stream")

    def test_the_event_shape_that_broke_0_15_0_projects_cleanly(self):
        """``tool_calls`` is a count on the agent-finished event (the live crash)."""
        from app.orchestrator.live import thought

        finished = {
            "seq": 9,
            "stage": "agent",
            "actor": "search",
            "title": "Web researcher finished: ok",
            "offset_ms": 812,
            "at": "2026-11-04T10:34:45+00:00",
            "detail": {"status": "ok", "latency_ms": 812, "wave": 1, "tool_calls": 1, "model_calls": 2},
        }
        out = thought(finished)
        self.assertEqual(out["kind"], "note")
        self.assertEqual(out["detail"]["status"], "ok")
        self.assertNotIn("tools", out["detail"])

    def test_the_bus_is_cleaned_up_after_the_stream(self):
        from app.orchestrator.runbus import BUS

        frames = self.frames({"message": "Explain closures in JavaScript."})
        run_id = frames[0][1]["run_id"]
        self.assertIsNone(BUS.get(run_id))
