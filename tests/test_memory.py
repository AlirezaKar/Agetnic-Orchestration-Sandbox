"""Harness memory: the conversation store, its files, and its CLI.

These tests write into a temporary directory, so they never touch the real
``memory/`` and never need a network.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from app.memory import (
    SessionMemory,
    format_search_answer,
    render_markdown,
    safe_id,
    session_view,
    summary_of,
    title_from,
    turn_text,
)
from scripts import memory as memory_cli


def a_response(answer: str = "An answer.", **extra) -> dict:
    payload = {
        "answer": answer,
        "provider": "fake",
        "model": "fake-1",
        "latency_ms": 42,
        "run_id": extra.pop("run_id", "run-1"),
        "sources": ["https://example.org/a"],
        "orchestration": {
            "run_id": extra.pop("run_id", "run-1"),
            "strategy": "auto",
            "synthesized": True,
            "provider": "fake",
            "model": "fake-1",
            "latency_ms": 42,
            "parallel_ms": 20,
            "model_calls": 3,
            "plan": {"intent": "general", "agents": ["general"], "steps": [], "strategy": "heuristic"},
            "waves": [{"wave": 1, "agents": ["general"], "depends_on": {}, "latency_ms": 20}],
            "trace": [{"seq": 1, "stage": "run", "actor": "manager", "title": "Request received"}],
            "trace_summary": {"events": 1},
        },
    }
    payload.update(extra)
    return payload


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "memory"
        self.memory = SessionMemory(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def reopen(self) -> SessionMemory:
        """A second store on the same directory = what a restart sees."""
        return SessionMemory(self.root)


class AppendTests(MemoryTestCase):
    def test_first_turn_creates_a_session_on_disk(self):
        info = self.memory.append(
            None, mode="auto", user_text="What is RAG?", response=a_response("Retrieval augmented generation.")
        )
        self.assertTrue(info["session_id"].startswith("s-"))
        self.assertEqual(info["turn_id"], "t-1")
        self.assertEqual(info["turns"], 1)
        self.assertTrue(info["stored"])
        self.assertTrue((self.root / "sessions" / f"{info['session_id']}.json").is_file())
        self.assertTrue((self.root / "sessions" / f"{info['session_id']}.md").is_file())
        self.assertTrue((self.root / "index.jsonl").is_file())

    def test_title_is_the_first_words_of_the_user(self):
        info = self.memory.append(None, mode="auto", user_text="Why is the sky blue?", response=a_response())
        self.assertEqual(self.memory.get(info["session_id"])["title"], "Why is the sky blue?")

    def test_second_turn_continues_the_same_session(self):
        first = self.memory.append(None, mode="auto", user_text="first question", response=a_response("one"))
        second = self.memory.append(
            first["session_id"], mode="auto", user_text="second question", response=a_response("two")
        )
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["turn_id"], "t-2")
        record = self.memory.get(first["session_id"])
        self.assertEqual(len(record["turns"]), 2)
        self.assertEqual(record["title"], "first question", "the title must not drift")
        self.assertEqual(record["stats"]["turns"], 2)
        self.assertEqual(record["stats"]["model_calls"], 6)

    def test_unknown_session_id_starts_a_new_one(self):
        info = self.memory.append("s-19990101-000000-abcdef", mode="auto", user_text="hi", response=a_response())
        self.assertNotEqual(info["session_id"], "s-19990101-000000-abcdef")
        self.assertTrue(self.memory.get(info["session_id"]))

    def test_turn_keeps_the_payload_the_browser_rendered(self):
        info = self.memory.append(None, mode="auto", user_text="q", response=a_response("answer text"))
        turn = self.memory.get(info["session_id"])["turns"][0]
        self.assertEqual(turn["user"]["text"], "q")
        self.assertTrue(turn["assistant"]["ok"])
        self.assertEqual(turn["assistant"]["answer"], "answer text")
        self.assertEqual(turn["assistant"]["response"]["orchestration"]["plan"]["agents"], ["general"])
        self.assertEqual(turn["assistant"]["agents"], ["general"])
        self.assertEqual(turn["assistant"]["trace_steps"], 1)

    def test_attached_files_are_remembered_by_name(self):
        info = self.memory.append(
            None,
            mode="auto",
            user_text="summarise this",
            user_files=[{"id": "f-1", "name": "policy.md", "kind": "text", "chars": 120}],
            response=a_response(),
        )
        files = self.memory.get(info["session_id"])["turns"][0]["user"]["files"]
        self.assertEqual(files[0]["name"], "policy.md")

    def test_failed_turn_is_remembered_too(self):
        info = self.memory.append(
            None,
            mode="auto",
            user_text="boom",
            ok=False,
            error={"status": 502, "body": {"error": "provider", "message": "upstream died"}},
        )
        turn = self.memory.get(info["session_id"])["turns"][0]
        self.assertFalse(turn["assistant"]["ok"])
        self.assertEqual(turn["assistant"]["error"]["status"], 502)
        self.assertIn("upstream died", self.memory.export_markdown(info["session_id"]))

    def test_search_turn_gets_the_text_the_page_shows(self):
        payload = {
            "provider": "ddg",
            "count": 1,
            "latency_ms": 12,
            "results": [{"title": "T", "url": "https://example.org", "snippet": "s"}],
        }
        info = self.memory.append(None, mode="search", user_text="python", response=payload)
        turn = self.memory.get(info["session_id"])["turns"][0]
        self.assertEqual(turn["assistant"]["text"], format_search_answer(payload))
        self.assertIn("Search backend: ddg", turn["assistant"]["text"])
        self.assertIn("1. T", turn["assistant"]["text"])

    def test_oldest_turns_are_dropped_past_the_cap(self):
        memory = SessionMemory(self.root, max_turns=3)
        session_id = None
        for index in range(5):
            info = memory.append(session_id, mode="auto", user_text=f"q{index}", response=a_response(f"a{index}"))
            session_id = info["session_id"]
        record = memory.get(session_id)
        self.assertEqual(len(record["turns"]), 3)
        self.assertEqual(record["turns"][-1]["user"]["text"], "q4")

    def test_disabled_memory_stores_nothing(self):
        memory = SessionMemory(self.root, enabled=False)
        self.assertIsNone(memory.append(None, mode="auto", user_text="hi", response=a_response()))
        self.assertEqual(memory.list(), [])
        self.assertFalse(memory.stats()["enabled"])
        self.assertEqual(memory.history(None), [])

    def test_a_directory_removed_while_running_heals_itself(self):
        info = self.memory.append(None, mode="auto", user_text="one", response=a_response())
        import shutil

        shutil.rmtree(self.root)
        again = self.memory.append(info["session_id"], mode="auto", user_text="two", response=a_response())
        self.assertTrue(again["stored"], "the store must recreate its directory instead of dropping turns")
        self.assertEqual(len(self.reopen().get(info["session_id"])["turns"]), 2)

    def test_unwritable_directory_keeps_the_session_in_process(self):
        blocker = Path(self._tmp.name) / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        memory = SessionMemory(blocker / "memory")
        self.assertFalse(memory.writable)
        info = memory.append(None, mode="auto", user_text="still here", response=a_response())
        self.assertIsNotNone(info)
        self.assertFalse(info["stored"])
        self.assertIsNotNone(memory.get(info["session_id"]))


class ReadTests(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.memory.append(
            None, mode="auto", user_text="refund policy?", response=a_response("Refunds in 30 days.")
        )
        self.memory.append(
            self.first["session_id"],
            mode="agent",
            user_text="and the sky?",
            response=a_response("Rayleigh scattering."),
        )
        self.other = self.memory.append(
            None, mode="search", user_text="python asyncio", response={"provider": "ddg", "count": 0, "results": []}
        )

    def test_list_is_newest_first_with_summaries(self):
        items = self.memory.list()
        self.assertEqual(items[0]["session_id"], self.other["session_id"])
        second = items[1]
        self.assertEqual(second["session_id"], self.first["session_id"])
        self.assertEqual(second["turns"], 2)
        self.assertEqual(second["modes"], ["auto", "agent"])
        self.assertIn("Rayleigh", second["last_answer"])

    def test_list_filters_on_title_and_text(self):
        self.assertEqual(len(self.memory.list(query="refund")), 1)
        self.assertEqual(self.memory.list(query="nothing-matches"), [])

    def test_history_is_alternating_and_ordered(self):
        history = self.memory.history(self.first["session_id"])
        self.assertEqual([item["role"] for item in history], ["user", "assistant", "user", "assistant"])
        self.assertEqual(history[0]["content"], "refund policy?")
        self.assertEqual(history[1]["content"], "Refunds in 30 days.")
        self.assertEqual(history[3]["content"], "Rayleigh scattering.")

    def test_history_respects_the_turn_limit(self):
        history = self.memory.history(self.first["session_id"], turns=1)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "and the sky?")

    def test_history_skips_failed_turns(self):
        failed = self.memory.append(None, mode="auto", user_text="boom", ok=False, error={"body": {"message": "x"}})
        self.assertEqual(self.memory.history(failed["session_id"]), [{"role": "user", "content": "boom"}])

    def test_history_clips_long_answers(self):
        memory = SessionMemory(self.root, history_chars=200)
        info = memory.append(None, mode="auto", user_text="long", response=a_response("x" * 900))
        history = memory.history(info["session_id"])
        self.assertLess(len(history[1]["content"]), 300)
        self.assertIn("+700 chars", history[1]["content"])

    def test_history_of_an_unknown_session_is_empty(self):
        self.assertEqual(self.memory.history("s-00000000-000000-000000"), [])

    def test_restart_sees_everything(self):
        reopened = self.reopen()
        record = reopened.get(self.first["session_id"])
        self.assertEqual(len(record["turns"]), 2)
        self.assertEqual(reopened.list()[0]["session_id"], self.other["session_id"])
        self.assertEqual(reopened.history(self.first["session_id"])[1]["content"], "Refunds in 30 days.")

    def test_stats_count_sessions_and_turns(self):
        stats = self.memory.stats()
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["turns"], 3)
        self.assertTrue(stats["writable"])
        self.assertEqual(stats["dir"], str(self.root))


class ManageTests(MemoryTestCase):
    def test_rename(self):
        info = self.memory.append(None, mode="auto", user_text="old title", response=a_response())
        record = self.memory.rename(info["session_id"], "Refund deep dive")
        self.assertEqual(record["title"], "Refund deep dive")
        self.assertEqual(self.reopen().get(info["session_id"])["title"], "Refund deep dive")

    def test_rename_of_an_unknown_session_is_none(self):
        self.assertIsNone(self.memory.rename("s-00000000-000000-000000", "x"))

    def test_delete_removes_files_and_index_entry(self):
        info = self.memory.append(None, mode="auto", user_text="forget me", response=a_response())
        self.assertTrue(self.memory.delete(info["session_id"]))
        self.assertFalse((self.root / "sessions" / f"{info['session_id']}.json").exists())
        self.assertFalse((self.root / "sessions" / f"{info['session_id']}.md").exists())
        self.assertIsNone(self.memory.get(info["session_id"]))
        self.assertEqual(self.reopen().list(), [], "a tombstone must survive a restart")
        self.assertFalse(self.memory.delete(info["session_id"]))

    def test_clear_keeps_the_session_but_drops_the_turns(self):
        info = self.memory.append(None, mode="auto", user_text="hi", response=a_response())
        record = self.memory.clear(info["session_id"])
        self.assertEqual(record["turns"], [])
        self.assertEqual(record["stats"]["turns"], 0)
        self.assertIsNotNone(self.memory.get(info["session_id"]))

    def test_forget_all(self):
        self.memory.append(None, mode="auto", user_text="a", response=a_response())
        self.memory.append(None, mode="auto", user_text="b", response=a_response())
        self.assertEqual(self.memory.forget_all(), 2)
        self.assertEqual(self.memory.list(), [])

    def test_create_returns_an_empty_session(self):
        record = self.memory.create(title="Fresh", mode="auto")
        self.assertEqual(record["turns"], [])
        self.assertEqual(self.memory.summary(record["session_id"])["title"], "Fresh")

    def test_turn_text_prefers_the_plain_answer(self):
        turn = {"assistant": {"ok": True, "answer": "A", "text": "B", "response": {"answer": "C"}}}
        self.assertEqual(turn_text(turn), "A")
        self.assertEqual(turn_text({"assistant": {"text": "B"}}), "B")
        self.assertEqual(turn_text({}), "")

    def test_session_view_explains_how_to_continue(self):
        info = self.memory.append(None, mode="auto", user_text="hi", response=a_response())
        view = session_view(self.memory.get(info["session_id"]))
        self.assertEqual(view["resume"]["turns"], 1)
        self.assertIn("session_id", view["resume"]["hint"])
        self.assertEqual(view["summary"]["turns"], 1)


class SafetyTests(MemoryTestCase):
    def test_unsafe_ids_never_become_paths(self):
        for attempt in ("../../etc/passwd", "s-1/../../x", "", None, "sess;rm -rf /"):
            self.assertIsNone(safe_id(attempt), attempt)
        self.assertEqual(safe_id("s-20260101-000000-abc123"), "s-20260101-000000-abc123")

    def test_traversal_returns_nothing_instead_of_a_file(self):
        self.assertIsNone(self.memory.get("../../etc/passwd"))
        self.assertFalse(self.memory.delete("../../etc/passwd"))
        self.assertEqual(self.memory.history("../../etc/passwd"), [])

    def test_titles_are_shortened_at_a_word_boundary(self):
        title = title_from("word " * 40)
        self.assertLessEqual(len(title), 76)
        self.assertTrue(title.endswith("…"))
        self.assertEqual(title_from("   "), "Untitled session")

    def test_index_reads_the_last_state_of_a_session(self):
        info = self.memory.append(None, mode="auto", user_text="one", response=a_response())
        self.memory.append(info["session_id"], mode="auto", user_text="two", response=a_response())
        items = self.memory.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["turns"], 2)

    def test_markdown_has_both_sides_and_the_run(self):
        info = self.memory.append(None, mode="auto", user_text="question", response=a_response("answer"))
        markdown = render_markdown(self.memory.get(info["session_id"]))
        self.assertIn("**You**", markdown)
        self.assertIn("**Assistant**", markdown)
        self.assertIn("question", markdown)
        self.assertIn("answer", markdown)
        self.assertIn("run-1", markdown)

    def test_corrupt_file_is_ignored(self):
        path = self.root / "sessions" / "s-20260101-000000-abc123.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.memory.get("s-20260101-000000-abc123"))

    def test_summary_of_an_empty_record_is_safe(self):
        self.assertEqual(summary_of({})["turns"], 0)


class CliTests(MemoryTestCase):
    def run_cli(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = memory_cli.main(["--memory-dir", str(self.root), *argv])
        return code, buffer.getvalue()

    def setUp(self):
        super().setUp()
        self.info = self.memory.append(
            None,
            mode="auto",
            user_text="What is the refund window?",
            response=a_response("Refunds are allowed within 30 days."),
        )

    def test_path(self):
        code, out = self.run_cli("path")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.root))

    def test_list_shows_the_session(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn(self.info["session_id"], out)
        self.assertIn("What is the refund window?", out)
        self.assertIn("1 session(s)", out)

    def test_show_prints_both_sides(self):
        code, out = self.run_cli("show", self.info["session_id"])
        self.assertEqual(code, 0)
        self.assertIn("you │ What is the refund window?", out)
        self.assertIn("ai │ Refunds are allowed within 30 days.", out)
        self.assertIn("run-1", out)

    def test_show_json_is_the_stored_record(self):
        code, out = self.run_cli("show", self.info["session_id"], "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["session_id"], self.info["session_id"])

    def test_show_unknown_session_fails(self):
        code, out = self.run_cli("show", "s-00000000-000000-000000")
        self.assertEqual(code, 1)
        self.assertIn("no session", out)

    def test_search_finds_a_turn(self):
        code, out = self.run_cli("search", "refund")
        self.assertEqual(code, 0)
        self.assertIn("turn 1", out)
        code, out = self.run_cli("search", "zebra")
        self.assertEqual(code, 1)

    def test_export_to_stdout_and_file(self):
        code, out = self.run_cli("export", self.info["session_id"])
        self.assertEqual(code, 0)
        self.assertIn("# What is the refund window?", out)
        target = Path(self._tmp.name) / "session.md"
        code, out = self.run_cli("export", self.info["session_id"], "-o", str(target))
        self.assertEqual(code, 0)
        self.assertIn("wrote", out)
        self.assertIn("Refunds are allowed", target.read_text(encoding="utf-8"))

    def test_forget_one_and_all(self):
        code, out = self.run_cli("forget", self.info["session_id"])
        self.assertEqual(code, 0)
        self.assertEqual(self.memory.list(), [])
        self.info = self.memory.append(None, mode="auto", user_text="b", response=a_response())
        code, out = self.run_cli("forget", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(self.memory.list(), [])

    def test_no_command_prints_help(self):
        code, _ = self.run_cli()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
