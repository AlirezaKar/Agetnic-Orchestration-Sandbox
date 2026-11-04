"""The live seam: the run bus and the thought projection.

These are the two pieces that let the page watch a run instead of waiting for
it: the bus carries trace events as they are written, and ``live.thought``
decides what the browser is allowed to see — and what it must never see (system
prompts, the final answer before the bubble).
"""

from __future__ import annotations

import unittest

from app.orchestrator.live import category_for, plain_thought, thought
from app.orchestrator.runbus import BUS, END, Channel, RunBus
from app.orchestrator.trace import RunTrace


def event(stage: str, actor: str, title: str, **detail):
    return {
        "seq": 1,
        "stage": stage,
        "actor": actor,
        "title": title,
        "offset_ms": 12,
        "at": "2026-11-04T09:00:00.000Z",
        "detail": detail,
    }


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_come_out_in_order(self):
        channel = Channel(run_id="run-1")
        channel.publish({"seq": 1})
        channel.publish({"seq": 2})
        self.assertEqual((await channel.next())[ "seq"], 1)
        self.assertEqual((await channel.next())["seq"], 2)

    async def test_close_ends_the_stream(self):
        channel = Channel(run_id="run-1")
        channel.publish({"seq": 1})
        channel.close()
        self.assertEqual((await channel.next())["seq"], 1)
        self.assertIs(await channel.next(), END)
        self.assertTrue(channel.closed)

    async def test_a_publish_after_close_is_ignored(self):
        channel = Channel(run_id="run-1")
        channel.close()
        channel.publish({"seq": 9})
        self.assertIs(await channel.next(), END)

    async def test_a_slow_reader_loses_the_oldest_events_not_the_run(self):
        channel = Channel(run_id="run-1", queue_size=2)
        for seq in range(5):
            channel.publish({"seq": seq})
        self.assertEqual(channel.dropped, 3)
        kinds = [(await channel.next())["seq"] for _ in range(2)]
        self.assertEqual(kinds, [3, 4])

    async def test_next_times_out_instead_of_hanging(self):
        channel = Channel(run_id="run-1")
        self.assertIsNone(await channel.next(timeout=0.01))

    async def test_history_is_bounded(self):
        channel = Channel(run_id="run-1", history_size=2)
        for seq in range(4):
            channel.publish({"seq": seq})
        self.assertEqual([item["seq"] for item in channel.events], [0, 1])


class RunBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_publish_close(self):
        bus = RunBus()
        channel = bus.open("run-a")
        bus.publish("run-a", {"seq": 1})
        bus.close("run-a")
        self.assertIs(bus.get("run-a"), channel)
        self.assertEqual((await channel.next())["seq"], 1)
        self.assertIs(await channel.next(), END)

    async def test_publishing_to_an_unknown_run_is_a_no_op(self):
        bus = RunBus()
        bus.publish("nobody", {"seq": 1})  # must not raise
        bus.close("nobody")
        self.assertEqual(len(bus), 0)

    async def test_forget_drops_the_channel(self):
        bus = RunBus()
        bus.open("run-a")
        bus.forget("run-a")
        self.assertIsNone(bus.get("run-a"))

    async def test_prune_keeps_live_channels_and_drops_expired_ones(self):
        bus = RunBus(ttl=10.0)
        live = bus.open("live")
        done = bus.open("done")
        done.close()
        removed = bus.prune(now=done.touched + 11.0)
        self.assertEqual(removed, 1)
        self.assertIs(bus.get("live"), live)
        self.assertIsNone(bus.get("done"))

    async def test_prune_caps_the_number_of_channels(self):
        bus = RunBus(channels=3)
        for index in range(6):
            bus.open(f"run-{index}")
        self.assertLessEqual(len(bus), 3)

    async def test_the_process_bus_is_a_singleton(self):
        self.assertIs(BUS, BUS)
        self.assertIsInstance(BUS, RunBus)


class TraceSinkTests(unittest.TestCase):
    def test_every_event_reaches_the_sink(self):
        seen: list[dict] = []
        trace = RunTrace(run_id="run-x", sink=seen.append)
        trace.add("run", "manager", "Request received", message="hi")
        trace.add("agent", "search", "Task received", task="look it up")
        self.assertEqual([item["title"] for item in seen], ["Request received", "Task received"])
        self.assertEqual(trace.run_id, "run-x")

    def test_a_broken_sink_never_breaks_the_run(self):
        def boom(_event):
            raise RuntimeError("watcher died")

        trace = RunTrace(sink=boom)
        event_out = trace.add("run", "manager", "still fine")
        self.assertEqual(event_out["title"], "still fine")
        self.assertEqual(len(trace.events), 1)


class CategoryTests(unittest.TestCase):
    def test_planner_events_group_under_the_planner(self):
        self.assertEqual(category_for(event("planner", "planner", "Delegating")), ("planner", "planner", "Planner"))

    def test_agent_and_tool_share_the_agents_group(self):
        agent = category_for(event("agent", "search", "Model reply"))
        tool = category_for(event("tool", "search", "web_search"))
        self.assertEqual(agent, ("agent", "agent:search", "Web researcher"))
        self.assertEqual(agent, tool)

    def test_each_agent_has_its_own_group(self):
        self.assertNotEqual(
            category_for(event("agent", "search", "x"))[1],
            category_for(event("agent", "data", "x"))[1],
        )

    def test_evaluator_and_manager_are_their_own_groups(self):
        self.assertEqual(category_for(event("evaluate", "evaluator", "x"))[:2], ("evaluator", "evaluator"))
        self.assertEqual(category_for(event("synthesis", "manager", "x"))[0], "manager")
        self.assertEqual(category_for(event("merge", "manager", "x"))[0], "manager")
        self.assertEqual(category_for(event("run", "manager", "x"))[0], "manager")

    def test_an_unknown_agent_still_gets_a_label(self):
        _category, group, label = category_for(event("agent", "mystery", "x"))
        self.assertEqual(group, "agent:mystery")
        self.assertEqual(label, "Mystery")


class ThoughtTests(unittest.TestCase):
    def test_a_prompt_thought_shows_the_task_not_the_instructions(self):
        out = thought(
            event(
                "agent",
                "search",
                "Prompt sent to the model (step 1)",
                messages=[
                    {"role": "system", "content": "SYSTEM INSTRUCTIONS - never show these"},
                    {"role": "user", "content": "Find the latest release."},
                ],
            )
        )
        self.assertEqual(out["kind"], "prompt")
        self.assertEqual(out["text"], "Find the latest release.")
        self.assertNotIn("SYSTEM INSTRUCTIONS", str(out))

    def test_a_reply_thought_carries_what_the_model_said(self):
        out = thought(event("agent", "general", "Model reply (step 1)", content="I will reason about it."))
        self.assertEqual(out["kind"], "reply")
        self.assertEqual(out["text"], "I will reason about it.")

    def test_the_final_answer_is_not_revealed_as_a_thought(self):
        out = thought(event("synthesis", "manager", "Final answer", content="THE WHOLE ANSWER"))
        self.assertEqual(out["kind"], "status")
        self.assertNotIn("THE WHOLE ANSWER", out["text"])
        self.assertIn("Wrote the answer", out["text"])

    def test_a_tool_thought_names_the_tool_and_the_query(self):
        out = thought(event("tool", "search", "web_search", tool="web_search", query="python 3.14"))
        self.assertEqual((out["kind"], out["text"]), ("tool", "web_search · python 3.14"))

    def test_a_failed_tool_is_named_once_and_kept_short(self):
        out = thought(event("tool", "search", "web_search failed", error="TLS blob " + "x" * 2000))
        self.assertTrue(out["text"].startswith("web_search failed: TLS blob"), out["text"][:60])
        self.assertLessEqual(len(out["text"]), 260)  # a line, not a log

    def test_a_failed_tool_says_so(self):
        out = thought(event("tool", "search", "web_search", tool="web_search", error="rate limited"))
        self.assertIn("failed", out["text"])
        self.assertIn("rate limited", out["text"])

    def test_a_verdict_thought_summarises_the_scores_and_the_gaps(self):
        out = thought(
            event(
                "evaluate",
                "evaluator",
                "Evaluator reviewed 2 report(s)",
                summary="one report was thin",
                gaps=["the release date"],
                items=[
                    {"agent": "search", "label": "Web researcher", "score": 3, "verdict": "revise"},
                    {"agent": "data", "label": "Data analyst", "score": 5, "verdict": "keep"},
                ],
            )
        )
        self.assertEqual(out["kind"], "verdict")
        self.assertIn("one report was thin", out["text"])
        self.assertIn("Web researcher 3/5", out["text"])
        self.assertIn("the release date", out["text"])
        self.assertEqual(len(out["detail"]["items"]), 2)

    def test_a_rewrite_thought_says_what_changed(self):
        out = thought(
            event(
                "evaluate",
                "evaluator",
                "Web researcher report rewritten",
                agent="search",
                issues=["buries the answer"],
                before="the rambling version",
                after="the tight version",
                chars_before=800,
                chars_after=300,
            )
        )
        self.assertEqual(out["kind"], "rewrite")
        self.assertIn("buries the answer", out["text"])
        self.assertIn("800 → 300", out["text"])

    def test_long_text_is_clipped(self):
        out = thought(event("agent", "general", "Model reply", content="x" * 5000), limit=100)
        self.assertLessEqual(len(out["text"]), 110)
        self.assertTrue(out["text"].endswith("…"))

    def test_the_projection_never_carries_prompts_or_system_text(self):
        out = thought(
            event(
                "agent",
                "search",
                "Prompt sent to the model",
                messages=[{"role": "system", "content": "SECRET"}, {"role": "user", "content": "task"}],
                api_key="nope",
            )
        )
        self.assertNotIn("messages", out["detail"])
        self.assertNotIn("api_key", out["detail"])
        self.assertNotIn("SECRET", str(out))

    def test_structured_detail_survives(self):
        out = thought(
            event("agent", "search", "finished", status="ok", latency_ms=812, wave=2, tool_calls=[{"name": "web_search"}])
        )
        self.assertEqual(out["detail"]["status"], "ok")
        self.assertEqual(out["detail"]["latency_ms"], 812)
        self.assertEqual(out["detail"]["wave"], 2)
        self.assertEqual(out["detail"]["tools"], ["web_search"])

    # The field that crashed a live 0.15.0 run: ``tool_calls`` is a *list* on a
    # model step and a plain *count* on the "agent finished" event. The stream
    # must survive either, and a thought may never raise.
    def test_tool_calls_as_a_count_does_not_break_the_projection(self):
        for count in (0, 1, 3):
            out = thought(
                event("agent", "search", "Web researcher finished: ok", status="ok", tool_calls=count)
            )
            self.assertEqual(out["kind"], "note")
            self.assertNotIn("tools", out["detail"])
            self.assertEqual(out["detail"]["status"], "ok")

    def test_tool_calls_as_a_list_still_becomes_the_tools_chip(self):
        out = thought(
            event("agent", "search", "finished", tool_calls=[{"name": "web_search"}, {"name": "fetch_url"}])
        )
        self.assertEqual(out["detail"]["tools"], ["web_search", "fetch_url"])

    def test_odd_shapes_in_the_detail_are_survived(self):
        """Fields that are sometimes rows are sometimes not: never a crash."""
        cases = [
            {"items": {"search": 4}},
            {"items": "nope"},
            {"items": 2},
            {"agents": 2},
            {"agents": {"search": "ok"}},
            {"tool_calls": "web_search"},
            {"tool_calls": 1},
            {"tool_calls": None},
            {"gaps": 3},
        ]
        for detail in cases:
            with self.subTest(detail=detail):
                out = thought(event("evaluate", "evaluator", "Evaluator reviewed 1 report(s)", **detail))
                self.assertIsInstance(out["detail"], dict)
                for key in ("items", "agents", "tools", "gaps"):
                    self.assertNotIn(key, out["detail"])

    def test_a_single_gap_written_as_text_is_still_shown(self):
        out = thought(event("evaluate", "evaluator", "reviewed", gaps="the publication date"))
        self.assertEqual(out["detail"]["gaps"], ["the publication date"])

    # The fallback the stream uses when the projection *does* fail: it cannot
    # raise, and it keeps the line the reader needs (who, when, what).
    def test_a_plain_thought_never_touches_the_detail(self):
        out = plain_thought(
            event("agent", "search", "Model reply (step 1)", content=object(), tool_calls=object())
        )
        self.assertEqual(out["kind"], "status")
        self.assertEqual(out["text"], "Model reply (step 1)")
        self.assertEqual(out["category"], "agent")
        self.assertEqual(out["detail"], {})

    def test_a_done_event_says_how_the_run_ended(self):
        out = thought(event("done", "manager", "Run finished", latency_ms=8100, model_calls=6))
        self.assertEqual(out["category"], "manager")
        self.assertEqual(out["text"], "Run finished")


class StreamOrderTests(unittest.IsolatedAsyncioTestCase):
    """The sink of a real trace, projected in order — the page's actual feed."""

    async def test_a_trace_becomes_a_planner_agent_evaluator_manager_feed(self):
        channel = Channel(run_id="run-live")
        trace = RunTrace(sink=channel.publish)
        trace.add("run", "manager", "Request received", message="hi")
        trace.add_prompt("planner", "planner", "Planner prompt", [{"role": "user", "content": "pick agents"}])
        trace.add_response("agent", "search", "Model reply", _Resp("looking"))
        trace.add("tool", "search", "web_search", tool="web_search", query="q")
        trace.add("evaluate", "evaluator", "Evaluator reviewed 1 report(s)", items=[{"agent": "search", "score": 4}])
        trace.add_response("synthesis", "manager", "Final answer", _Resp("the answer"))
        channel.close()
        feed = []
        while True:
            item = await channel.next()
            if item is END:
                break
            feed.append(thought(item))
        self.assertEqual(
            [item["category"] for item in feed],
            ["manager", "planner", "agent", "agent", "evaluator", "manager"],
        )
        self.assertEqual([item["kind"] for item in feed], ["status", "prompt", "reply", "tool", "verdict", "status"])


class _Resp:
    """The bits of a ModelResponse the trace reads."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []
        self.usage = {}
        self.finish_reason = "stop"
        self.model = "fake-1"
        self.provider = "fake"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
