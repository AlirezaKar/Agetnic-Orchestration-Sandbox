"""Orchestrator: planner routing, parallel executor, manager synthesis."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.config import Settings
from app.llm.base import ModelResponse, ToolCall
from app.orchestrator import agents as agents_module
from app.orchestrator.agents import (
    AgentContext,
    AgentOutcome,
    analyse_table,
    rank_chunks,
    run_data,
    run_document,
    _safe_arithmetic,
)
from app.orchestrator.executor import run_plan, schedule
from app.orchestrator.files import extract
from app.orchestrator.manager import Manager, _parse_review, deterministic_answer
from app.orchestrator import runlog
from app.orchestrator.planner import (
    AGENTS,
    Plan,
    PlanStep,
    _has_cycle,
    _parse_detail_plan,
    _parse_llm_plan,
    detail_plan,
    heuristic_plan,
    make_plan,
)
from app.orchestrator.trace import RunTrace, clip_event
from app.search.base import SearchResponse, SearchResult


def settings_for(tmp_dir: str = "/tmp/agetnic-tests", **overrides) -> Settings:
    base = dict(
        _env_file=None,
        orchestrator_planner="heuristic",
        orchestrator_synthesize=True,
        orchestrator_review=False,
        orchestrator_log_runs=False,
        orchestrator_timeout=5.0,
        orchestrator_max_parallel=4,
        llm_max_tokens=400,
        llm_temperature=0.2,
    )
    base.update(overrides)
    return Settings(**base)


def csv_file(name: str = "sales.csv", body: str = "month,revenue\nJan,10\nFeb,20\nMar,35\n"):
    return extract(name, body.encode())


def doc_file(name: str = "policy.md", body: str = "Refunds are allowed within 30 days."):
    return extract(name, body.encode())


class FakeModel:
    """Answers differently per role prompt, and records what it was asked."""

    def __init__(
        self,
        content: str = "answer",
        fail: bool = False,
        *,
        detail_plan: dict | None = None,
        review: dict | None = None,
    ):
        self.content = content
        self.fail = fail
        self.calls: list[list[dict]] = []
        self.detail_plan = detail_plan
        self.review = review if review is not None else {"note": "nothing missing", "follow_up": []}

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("provider exploded")
        system = messages[0]["content"] if messages else ""
        if "The manager has already decided" in system:
            # DETAIL_PROMPT — the planner writes one brief per selected agent
            steps = self.detail_plan
            if steps is None:
                steps = [
                    {"agent": "document", "task": "read the file", "reason": "file", "depends_on": []},
                    {"agent": "data", "task": "analyse the table", "reason": "table", "depends_on": []},
                    {"agent": "search", "task": "look it up", "reason": "fresh", "depends_on": []},
                    {"agent": "general", "task": "explain the findings", "reason": "reasoning",
                     "depends_on": []},
                ]
            return ModelResponse(
                content='{"summary": "written by the planner model", "steps": ' + json.dumps(steps) + "}",
                provider="fake",
                model="fake-1",
            )
        if "Decide which specialists" in system:
            # PLANNER_PROMPT — selection + briefs in one shot (strategy "llm")
            return ModelResponse(content=self.content, provider="fake", model="fake-1")
        if "planner of a small agent team" in system:
            steps = self.detail_plan
            if steps is None:
                steps = [
                    {"agent": "document", "task": "read the file", "reason": "file", "depends_on": []},
                    {"agent": "data", "task": "analyse the table", "reason": "table", "depends_on": []},
                    {"agent": "search", "task": "look it up", "reason": "fresh", "depends_on": []},
                    {"agent": "general", "task": "explain the findings", "reason": "reasoning",
                     "depends_on": []},
                ]
            return ModelResponse(
                content='{"summary": "written by the planner model", "steps": ' + json.dumps(steps) + "}",
                provider="fake",
                model="fake-1",
            )
        if "reviewing what came back" in system:
            return ModelResponse(
                content=json.dumps(self.review), provider="fake", model="fake-1"
            )
        if "manager of a small agent team" in system:
            text = "MANAGER: " + self.content
        elif "document analyst" in system:
            text = "DOCUMENT: " + self.content
        elif "data analyst" in system:
            text = "DATA: " + self.content
        else:
            text = "GENERAL: " + self.content
        return ModelResponse(
            content=text, provider="fake", model="fake-1", usage={"total_tokens": 7}
        )

    @property
    def prompts(self) -> list[str]:
        return [messages[-1]["content"] for messages in self.calls]


class FakeSearchChain:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query, limit=5):
        self.queries.append(query)
        return SearchResponse(
            query=query,
            provider="fake-search",
            results=[SearchResult(title="Result", url="https://example.org/a", snippet="snip")],
            latency_ms=3,
        )


class PlannerTests(unittest.TestCase):
    def plan(self, message, attachments=None, roster=None):
        return heuristic_plan(message, attachments or [], roster=roster)

    def test_current_information_uses_search(self):
        plan = self.plan("What is the latest Python release? Cite your sources.")
        self.assertIn("search", plan.agents)
        self.assertEqual(plan.intent, "research")

    def test_plain_explanation_stays_general(self):
        plan = self.plan("Explain how retrieval-augmented generation works.")
        self.assertEqual(plan.agents, ["general"])
        self.assertFalse(plan.to_dict()["parallel"])

    def test_arithmetic_numbers_use_data(self):
        plan = self.plan("What is the average of 12, 30, 45 and 60?")
        self.assertIn("data", plan.agents)

    def test_attachments_add_document_agent(self):
        plan = self.plan("Summarise this contract.", [doc_file()])
        self.assertIn("document", plan.agents)
        self.assertEqual(plan.intent, "document-qa")

    def test_intent_names_the_mix(self):
        self.assertEqual(self.plan("Explain RAG.").intent, "general")
        self.assertEqual(self.plan("latest news on X").intent, "research")
        self.assertEqual(self.plan("average of 1, 2, 3").intent, "analysis")
        self.assertEqual(
            self.plan("summarise the file and check the latest rules online", [doc_file()]).intent,
            "document-cross-check",
        )
        self.assertEqual(
            self.plan("total revenue in the attached csv", [csv_file()]).intent,
            "document-analysis",
        )

    def test_tabular_attachment_adds_data(self):
        plan = self.plan("Which month grew fastest?", [csv_file()])
        self.assertIn("data", plan.agents)
        self.assertIn("document", plan.agents)

    def test_multi_agent_plan_is_parallel(self):
        plan = self.plan(
            "Summarise the attached policy and check the latest rules online.",
            [doc_file()],
        )
        self.assertIn("document", plan.agents)
        self.assertIn("search", plan.agents)
        self.assertTrue(plan.to_dict()["parallel"])

    def test_roster_restricts_agents(self):
        plan = self.plan("What is the latest Python release?", roster=["general"])
        self.assertEqual(plan.agents, ["general"])
        self.assertEqual(plan.notes, [])

    def test_every_step_has_task_and_reason(self):
        plan = self.plan("Summarise the attached file and compute the total.", [csv_file()])
        for step in plan.steps:
            self.assertTrue(step.task.strip())
            self.assertTrue(step.reason.strip())
            self.assertIn(step.agent, AGENTS)

    def test_plan_serialises(self):
        data = self.plan("Summarise the attached file.", [doc_file()]).to_dict()
        for key in ("intent", "summary", "strategy", "steps", "agents", "files", "parallel"):
            self.assertIn(key, data)
        self.assertEqual(data["files"][0]["name"], "policy.md")

    def test_generalist_is_not_added_to_a_plain_lookup(self):
        plan = self.plan("What is the latest Python release?")
        self.assertEqual(plan.agents, ["search"])

    def test_generalist_joins_when_reasoning_is_asked_for(self):
        plan = self.plan("Explain how the latest Python release differs from 3.11.")
        self.assertEqual(plan.agents, ["search", "general"])

    def test_no_duplicate_agents(self):
        plan = self.plan("latest news, average prices, csv data")
        self.assertEqual(len(plan.agents), len(set(plan.agents)))

    def test_parse_llm_plan_valid(self):
        raw = json.dumps(
            {
                "steps": [
                    {"agent": "search", "task": "find the release", "reason": "needs freshness"},
                    {"agent": "general", "task": "explain it", "reason": "wrap-up"},
                ]
            }
        )
        steps = _parse_llm_plan(raw, list(AGENTS))
        self.assertEqual([s.agent for s in steps], ["search", "general"])

    def test_parse_llm_plan_filters_unknown_and_duplicates(self):
        raw = json.dumps(
            {
                "steps": [
                    {"agent": "search", "task": "a"},
                    {"agent": "search", "task": "b"},
                    {"agent": "hacker", "task": "c"},
                ]
            }
        )
        steps = _parse_llm_plan(raw, list(AGENTS))
        self.assertEqual(len(steps), 1)

    def test_parse_llm_plan_rejects_noise(self):
        self.assertIsNone(_parse_llm_plan("no json here", list(AGENTS)))
        self.assertIsNone(_parse_llm_plan('{"steps": []}', list(AGENTS)))
        self.assertIsNone(_parse_llm_plan('{"steps": "nope"}', list(AGENTS)))
        self.assertIsNone(_parse_llm_plan("{broken", list(AGENTS)))

    def test_parse_llm_plan_respects_roster(self):
        raw = json.dumps({"steps": [{"agent": "search", "task": "a"}]})
        self.assertIsNone(_parse_llm_plan(raw, ["general"]))


class MakePlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_heuristic_by_default(self):
        plan = await make_plan("Explain RAG.", settings=settings_for(), model_chain=FakeModel())
        self.assertEqual(plan.strategy, "heuristic")

    async def test_llm_strategy_uses_model_plan(self):
        model = FakeModel()
        model.content = json.dumps({"steps": [{"agent": "search", "task": "look it up"}]})
        plan = await make_plan(
            "What is the latest release?",
            settings=settings_for(orchestrator_planner="llm"),
            model_chain=model,
        )
        self.assertEqual(plan.strategy, "llm")
        self.assertEqual(plan.agents, ["search"])

    async def test_llm_strategy_falls_back_on_garbage(self):
        model = FakeModel(content="I would rather chat about it.")
        plan = await make_plan(
            "What is the latest release?",
            settings=settings_for(orchestrator_planner="llm"),
            model_chain=model,
        )
        self.assertEqual(plan.strategy, "heuristic")
        self.assertTrue(any("heuristic" in note.lower() for note in plan.notes))

    async def test_llm_strategy_survives_provider_error(self):
        plan = await make_plan(
            "What is the latest release?",
            settings=settings_for(orchestrator_planner="llm"),
            model_chain=FakeModel(fail=True),
        )
        self.assertEqual(plan.strategy, "heuristic")


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def context(self, message="Summarise the file", attachments=None, model=None, **kwargs):
        return AgentContext(
            message=message,
            attachments=attachments or [],
            model_chain=model,
            search_chain=FakeSearchChain(),
            settings=settings_for(**kwargs),
        )

    def test_rank_chunks_prefers_relevant_text(self):
        attachment = extract("long.md", ("filler line\n" * 200 + "refund window is 30 days").encode())
        ranked = rank_chunks([attachment], "what is the refund window", limit=3)
        self.assertTrue(ranked)
        self.assertIn("refund window", " ".join(chunk for _a, chunk, _s in ranked))

    def test_safe_arithmetic(self):
        self.assertEqual(_safe_arithmetic("12 + 30 + 45?")[1], 87.0)
        self.assertEqual(_safe_arithmetic("(10 + 2) / 3")[1], 4.0)
        self.assertIsNone(_safe_arithmetic("hello 12"))
        self.assertIsNone(_safe_arithmetic("2 ** 8"))

    def test_table_profile(self):
        profile = analyse_table(csv_file())
        self.assertEqual(profile["rows"], 3)
        self.assertEqual(profile["numeric"]["revenue"]["sum"], 65.0)
        self.assertEqual(profile["numeric"]["revenue"]["median"], 20.0)

    async def test_document_agent_quotes_files(self):
        model = FakeModel(content="30 days")
        outcome = await run_document(
            PlanStep(agent="document", task="find the window", reason="files"),
            self.context(attachments=[doc_file()], model=model),
        )
        self.assertEqual(outcome.status, "ok")
        self.assertIn("30 days", outcome.answer)
        self.assertIn("policy.md", model.prompts[0])

    async def test_document_agent_without_model_returns_extract(self):
        outcome = await run_document(
            PlanStep(agent="document", task="find it", reason="files"),
            self.context(attachments=[doc_file()], model=None),
        )
        self.assertEqual(outcome.status, "ok")
        self.assertIn("Refunds are allowed", outcome.answer)
        self.assertIn("no model", outcome.answer)

    async def test_document_agent_skips_without_files(self):
        outcome = await run_document(
            PlanStep(agent="document", task="find it", reason="files"), self.context()
        )
        self.assertEqual(outcome.status, "skipped")

    async def test_data_agent_computes_before_narrating(self):
        model = FakeModel(content="revenue rises")
        outcome = await run_data(
            PlanStep(agent="data", task="average revenue", reason="table"),
            self.context(message="average revenue per month", attachments=[csv_file()], model=model),
        )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.data["tables"][0]["numeric"]["revenue"]["sum"], 65.0)
        self.assertIn("21.666667", model.prompts[0])

    async def test_data_agent_handles_pure_arithmetic_without_model(self):
        outcome = await run_data(
            PlanStep(agent="data", task="add", reason="numbers"),
            self.context(message="12 + 30 + 45", model=None),
        )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.data["expression"]["value"], 87.0)

    async def test_data_agent_without_numbers_says_so(self):
        outcome = await run_data(
            PlanStep(agent="data", task="analyse", reason="asked"),
            self.context(message="tell me about it", model=None),
        )
        self.assertIn("No numeric data", outcome.answer)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_keep_plan_order(self):
        async def slow(step, ctx):
            await asyncio.sleep(0.05)
            return AgentOutcome(agent=step.agent, status="ok", answer=step.task)

        plan = Plan(
            intent="test",
            summary="two",
            steps=[
                PlanStep(agent="document", task="first", reason="r"),
                PlanStep(agent="general", task="second", reason="r"),
            ],
        )
        with mock.patch.dict(agents_module.AGENT_RUNNERS, {"document": slow, "general": slow}):
            outcomes = await run_plan(plan, AgentContext("m", [], None, None, settings_for()))
        self.assertEqual([o.answer for o in outcomes], ["first", "second"])

    async def test_agents_run_in_parallel(self):
        async def sleepy(step, ctx):
            await asyncio.sleep(0.25)
            return AgentOutcome(agent=step.agent, status="ok", answer="done")

        plan = Plan(
            intent="test",
            summary="three agents",
            steps=[PlanStep(agent=name, task="t", reason="r") for name in ("document", "search", "data")],
        )
        runners = {name: sleepy for name in ("document", "search", "data")}
        started = asyncio.get_running_loop().time()
        with mock.patch.dict(agents_module.AGENT_RUNNERS, runners):
            outcomes = await run_plan(
                plan,
                AgentContext("m", [], None, None, settings_for(orchestrator_max_parallel=3)),
            )
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(len(outcomes), 3)
        self.assertLess(elapsed, 0.6)  # 0.75 s if it were serial

    async def test_parallel_cap_limits_concurrency(self):
        active = {"now": 0, "max": 0}

        async def tracked(step, ctx):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return AgentOutcome(agent=step.agent, status="ok", answer="x")

        plan = Plan(
            intent="test",
            summary="cap",
            steps=[PlanStep(agent=name, task="t", reason="r") for name in ("document", "search", "data", "general")],
        )
        patch = {name: tracked for name in ("document", "search", "data", "general")}
        with mock.patch.dict(agents_module.AGENT_RUNNERS, patch):
            await run_plan(plan, AgentContext("m", [], None, None, settings_for(orchestrator_max_parallel=2)))
        self.assertLessEqual(active["max"], 2)

    async def test_crash_isolated_to_one_agent(self):
        async def boom(step, ctx):
            raise RuntimeError("kaboom")

        async def fine(step, ctx):
            return AgentOutcome(agent=step.agent, status="ok", answer="still here")

        plan = Plan(
            intent="test",
            summary="mixed",
            steps=[
                PlanStep(agent="search", task="explode", reason="r"),
                PlanStep(agent="general", task="work", reason="r"),
            ],
        )
        with mock.patch.dict(agents_module.AGENT_RUNNERS, {"search": boom, "general": fine}):
            outcomes = await run_plan(plan, AgentContext("m", [], None, None, settings_for()))
        self.assertEqual(outcomes[0].status, "failed")
        self.assertIn("kaboom", outcomes[0].error)
        self.assertEqual(outcomes[1].status, "ok")

    async def test_timeout_is_reported_not_raised(self):
        async def stalled(step, ctx):
            await asyncio.sleep(5)
            return AgentOutcome(agent=step.agent, status="ok")

        plan = Plan(intent="test", summary="slow", steps=[PlanStep(agent="search", task="t", reason="r")])
        with mock.patch.dict(agents_module.AGENT_RUNNERS, {"search": stalled}):
            outcomes = await run_plan(
                plan, AgentContext("m", [], None, None, settings_for(orchestrator_timeout=0.1))
            )
        self.assertEqual(outcomes[0].status, "failed")
        self.assertIn("timed out", outcomes[0].error)

    async def test_unknown_agent_fails_cleanly(self):
        plan = Plan(intent="test", summary="ghost", steps=[PlanStep(agent="ghost", task="t", reason="r")])
        outcomes = await run_plan(plan, AgentContext("m", [], None, None, settings_for()))
        self.assertEqual(outcomes[0].status, "failed")
        self.assertIn("unknown agent", outcomes[0].error)

    async def test_empty_plan_yields_nothing(self):
        plan = Plan(intent="test", summary="none", steps=[])
        self.assertEqual(await run_plan(plan, AgentContext("m", [], None, None, settings_for())), [])


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_run_plans_parallelises_and_synthesises(self):
        model = FakeModel(content="ok")
        settings = settings_for(orchestrator_planner="auto")
        manager = Manager(settings=settings, model_chain=model, search_chain=FakeSearchChain())
        result = await manager.run(
            "Summarise the attached policy and compute the total revenue in sales.csv.",
            attachments=[doc_file(), csv_file()],
        )
        agents = [outcome.agent for outcome in result.results]
        self.assertIn("document", agents)
        self.assertIn("data", agents)
        self.assertTrue(result.synthesized)
        self.assertTrue(result.answer.startswith("MANAGER:"))
        self.assertEqual(result.plan.strategy, "auto")
        self.assertEqual(result.plan.detail_source, "llm")
        # one planner call + one call per agent + one synthesis — no per-agent loop
        self.assertEqual(len(model.calls), len(result.results) + 2)
        self.assertEqual(result.model_calls, len(model.calls))

    async def test_serialised_payload_shape(self):
        manager = Manager(
            settings=settings_for(), model_chain=FakeModel(), search_chain=FakeSearchChain()
        )
        payload = (
            await manager.run("Explain closures in JavaScript.", attachments=[])
        ).to_dict()
        self.assertEqual(payload["mode"], "auto")
        orch = payload["orchestration"]
        for key in ("strategy", "synthesized", "plan", "agents", "parallel_ms", "latency_ms", "files"):
            self.assertIn(key, orch)
        self.assertIsInstance(orch["plan"]["summary"], str)
        self.assertIn("answer", payload)

    async def test_synthesis_can_be_disabled(self):
        model = FakeModel()
        manager = Manager(
            settings=settings_for(orchestrator_synthesize=False),
            model_chain=model,
            search_chain=FakeSearchChain(),
        )
        result = await manager.run("Explain event loops.", attachments=[])
        self.assertFalse(result.synthesized)
        self.assertIn("Generalist", result.answer)
        self.assertEqual(len(model.calls), 1)  # only the generalist

    async def test_synthesis_failure_falls_back(self):
        model = FakeModel()
        manager = Manager(settings=settings_for(), model_chain=model, search_chain=FakeSearchChain())

        original = model.complete

        async def broken(messages, tools=None, **kwargs):
            if "manager of a small agent team" in messages[0]["content"]:
                raise RuntimeError("synthesis down")
            return await original(messages, tools, **kwargs)

        model.complete = broken
        result = await manager.run("Explain event loops.", attachments=[])
        self.assertFalse(result.synthesized)
        self.assertIn("Generalist", result.answer)
        self.assertTrue(any("Synthesis" in note for note in result.notes))

    async def test_no_model_still_produces_an_answer(self):
        manager = Manager(settings=settings_for(), model_chain=None, search_chain=FakeSearchChain())
        result = await manager.run(
            "Summarise the attached policy.", attachments=[doc_file()]
        )
        self.assertFalse(result.synthesized)
        self.assertIn("Refunds are allowed", result.answer)
        self.assertTrue(any("no model" in note.lower() for note in result.notes))

    async def test_data_agent_numbers_reach_the_answer(self):
        manager = Manager(
            settings=settings_for(orchestrator_synthesize=False), model_chain=None, search_chain=None
        )
        result = await manager.run("average revenue per month", attachments=[csv_file()])
        self.assertIn("21.666667", result.answer)

    async def test_errors_are_collected(self):
        async def boom(step, ctx):
            raise RuntimeError("nope")

        manager = Manager(settings=settings_for(), model_chain=FakeModel(), search_chain=None)
        with mock.patch.dict(agents_module.AGENT_RUNNERS, {"general": boom}):
            result = await manager.run("Explain event loops.", attachments=[])
        self.assertEqual(result.results[0].status, "failed")
        self.assertTrue(result.errors)
        self.assertTrue(result.answer.startswith("No agent could answer"))

    async def test_sources_deduplicated(self):
        manager = Manager(
            settings=settings_for(), model_chain=None, search_chain=FakeSearchChain()
        )
        result = await manager.run("What is the latest release?", attachments=[])
        self.assertEqual(result.sources, ["https://example.org/a"])

    async def test_agent_subset_override(self):
        manager = Manager(
            settings=settings_for(), model_chain=FakeModel(), search_chain=FakeSearchChain()
        )
        result = await manager.run(
            "What is the latest release?", attachments=[doc_file()], agents=["search"]
        )
        self.assertEqual([outcome.agent for outcome in result.results], ["search"])

    async def test_latency_recorded(self):
        manager = Manager(
            settings=settings_for(), model_chain=FakeModel(), search_chain=FakeSearchChain()
        )
        result = await manager.run("Explain event loops.", attachments=[])
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertGreaterEqual(result.parallel_ms, 0)


class SearchTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_calls_land_in_the_trace(self):
        class ToolCallingModel:
            def __init__(self):
                self.n = 0

            async def complete(self, messages, tools=None, **kwargs):
                self.n += 1
                if self.n == 1:
                    return ModelResponse(
                        content="",
                        tool_calls=[ToolCall(name="web_search", arguments={"query": "python 3.14"})],
                        provider="fake",
                        model="fake-1",
                    )
                return ModelResponse(content="Python 3.14 is out.", provider="fake", model="fake-1")

        from app.orchestrator.agents import run_search

        trace = RunTrace()
        ctx = AgentContext(
            message="What is the latest Python release?",
            attachments=[],
            model_chain=ToolCallingModel(),
            search_chain=FakeSearchChain(),
            settings=settings_for(agent_max_steps=3),
            trace=trace,
        )
        outcome = await run_search(PlanStep(agent="search", task="find it", reason="fresh"), ctx)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.model_calls, 2)
        self.assertEqual(outcome.tool_calls, 1)
        self.assertEqual(outcome.sources, ["https://example.org/a"])
        events = trace.to_list()
        titles = [event["title"] for event in events]
        self.assertTrue(any(title.startswith("Tool call: web_search") for title in titles))
        self.assertTrue(any(title.startswith("Tool result: web_search") for title in titles))
        tool_call = next(e for e in events if e["title"] == "Tool call: web_search")
        self.assertEqual(tool_call["detail"]["arguments"], {"query": "python 3.14"})
        result = next(e for e in events if e["title"] == "Tool result: web_search")
        self.assertEqual(result["detail"]["output"]["results"][0]["url"], "https://example.org/a")
        prompts = [e for e in events if e["detail"].get("messages")]
        self.assertGreaterEqual(len(prompts), 2)


class DeterministicMergeTests(unittest.TestCase):
    def test_reports_are_labelled_and_failures_listed(self):
        answer = deterministic_answer(
            "q",
            [
                AgentOutcome(agent="document", status="ok", answer="refunds: 30 days"),
                AgentOutcome(agent="search", status="failed", error="boom"),
            ],
        )
        self.assertIn("Document analyst", answer)
        self.assertIn("refunds: 30 days", answer)
        self.assertIn("Unavailable: Web researcher", answer)

    def test_empty_result_explains_itself(self):
        answer = deterministic_answer(
            "q", [AgentOutcome(agent="data", status="failed", error="no numbers")]
        )
        self.assertIn("No agent could answer", answer)
        self.assertIn("no numbers", answer)


if __name__ == "__main__":
    unittest.main()


class DetailPlanTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_one_brief_per_selected_agent(self):
        raw = json.dumps(
            {
                "summary": "Search first, then explain.",
                "steps": [
                    {"agent": "search", "task": "find it", "reason": "fresh", "depends_on": []},
                    {"agent": "general", "task": "explain it", "reason": "reasoning",
                     "depends_on": ["search"]},
                ],
            }
        )
        parsed = _parse_detail_plan(raw, ["search", "general"])
        self.assertEqual(parsed["summary"], "Search first, then explain.")
        self.assertEqual(parsed["steps"]["general"]["depends_on"], ["search"])

    def test_rejects_missing_agent(self):
        raw = json.dumps({"steps": [{"agent": "search", "task": "a"}]})
        self.assertIsNone(_parse_detail_plan(raw, ["search", "general"]))

    def test_unknown_agent_is_dropped(self):
        raw = json.dumps(
            {"steps": [{"agent": "search", "task": "a"}, {"agent": "hacker", "task": "b"}]}
        )
        parsed = _parse_detail_plan(raw, ["search"])
        self.assertEqual(list(parsed["steps"]), ["search"])

    def test_rejects_cycles(self):
        raw = json.dumps(
            {
                "steps": [
                    {"agent": "search", "task": "a", "depends_on": ["data"]},
                    {"agent": "data", "task": "b", "depends_on": ["search"]},
                ]
            }
        )
        self.assertIsNone(_parse_detail_plan(raw, ["search", "data"]))

    def test_drops_self_and_unknown_dependencies(self):
        raw = json.dumps(
            {
                "steps": [
                    {"agent": "search", "task": "a", "depends_on": ["search", "nobody"]},
                    {"agent": "data", "task": "b", "depends_on": ["search"]},
                ]
            }
        )
        parsed = _parse_detail_plan(raw, ["search", "data"])
        self.assertEqual(parsed["steps"]["search"]["depends_on"], [])
        self.assertEqual(parsed["steps"]["data"]["depends_on"], ["search"])

    def test_cycle_helper(self):
        self.assertTrue(
            _has_cycle({"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}})
        )
        self.assertFalse(_has_cycle({"a": {"depends_on": []}, "b": {"depends_on": ["a"]}}))

    async def test_auto_strategy_writes_briefs_with_the_model(self):
        model = FakeModel(
            detail_plan=[
                {"agent": "search", "task": "Find the release notes.", "reason": "fresh",
                 "depends_on": []},
                {"agent": "general", "task": "Explain the changes using the findings.",
                 "reason": "needs context", "depends_on": ["search"]},
            ]
        )
        plan = await make_plan(
            "Explain how the latest release differs.",
            settings=settings_for(orchestrator_planner="auto"),
            model_chain=model,
        )
        self.assertEqual(plan.strategy, "auto")
        self.assertEqual(plan.detail_source, "llm")
        self.assertEqual(plan.steps[0].task, "Find the release notes.")
        self.assertEqual(plan.steps[1].depends_on, ["search"])

    async def test_auto_falls_back_when_the_model_returns_junk(self):
        model = FakeModel(detail_plan=[{"agent": "search", "task": "only one"}])
        plan = await make_plan(
            "Summarise this and search the web.",
            [doc_file()],
            settings=settings_for(orchestrator_planner="auto"),
            model_chain=model,
        )
        self.assertEqual(plan.strategy, "heuristic")
        self.assertEqual(plan.detail_source, "rules")
        self.assertTrue(any("brief" in note for note in plan.notes))

    async def test_detail_plan_traces_prompt_and_reply(self):
        trace = RunTrace()
        steps = [PlanStep(agent="general", task="t", reason="r")]
        await detail_plan(
            "hi", [], steps, FakeModel(), settings_for(orchestrator_planner="auto"), trace=trace
        )
        stages = [event["title"] for event in trace.events]
        self.assertIn("Planner prompt (writes each agent's brief)", stages)
        self.assertIn("Planner reply", stages)
        self.assertIn("Plan accepted", stages)


class ScheduleTests(unittest.TestCase):
    def test_independent_agents_share_wave_one(self):
        plan = Plan(
            intent="x",
            summary="s",
            steps=[
                PlanStep(agent="document", task="t", reason="r"),
                PlanStep(agent="data", task="t", reason="r"),
            ],
        )
        waves = schedule(plan)
        self.assertEqual([[step.agent for step in wave] for wave in waves], [["document", "data"]])
        self.assertEqual([step.wave for step in plan.steps], [1, 1])

    def test_dependencies_create_waves(self):
        plan = Plan(
            intent="x",
            summary="s",
            steps=[
                PlanStep(agent="search", task="t", reason="r"),
                PlanStep(agent="document", task="t", reason="r"),
                PlanStep(agent="general", task="t", reason="r", depends_on=["search", "document"]),
            ],
        )
        waves = schedule(plan)
        self.assertEqual([sorted(step.agent for step in wave) for wave in waves],
                         [["document", "search"], ["general"]])
        self.assertEqual(plan.steps[2].wave, 2)

    def test_cycle_still_terminates(self):
        plan = Plan(
            intent="x",
            summary="s",
            steps=[
                PlanStep(agent="search", task="t", reason="r", depends_on=["data"]),
                PlanStep(agent="data", task="t", reason="r", depends_on=["search"]),
            ],
        )
        waves = schedule(plan)
        self.assertEqual(len(waves), 1)

    def test_heuristic_generalist_depends_on_specialists(self):
        plan = heuristic_plan("Explain how the latest Python release differs from 3.11.")
        general = next(step for step in plan.steps if step.agent == "general")
        self.assertIn("search", general.depends_on)
        self.assertTrue(plan.to_dict()["waves"])


class DependencyExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_result_reaches_the_later_agent(self):
        seen: dict[str, str] = {}

        async def fake_document(step, ctx):
            return AgentOutcome(agent="document", status="ok", answer="DOC FINDING: 30 days")

        async def fake_general(step, ctx):
            block, used = ctx.upstream_block(step)
            seen["block"] = block
            seen["used"] = ",".join(used)
            return AgentOutcome(agent="general", status="ok", answer="ok")

        plan = Plan(
            intent="x",
            summary="s",
            steps=[
                PlanStep(agent="document", task="read", reason="r"),
                PlanStep(agent="general", task="explain", reason="r", depends_on=["document"]),
            ],
        )
        ctx = AgentContext("m", [], None, None, settings_for())
        with mock.patch.dict(
            agents_module.AGENT_RUNNERS, {"document": fake_document, "general": fake_general}
        ):
            outcomes = await run_plan(plan, ctx)
        self.assertEqual([o.agent for o in outcomes], ["document", "general"])
        self.assertIn("DOC FINDING: 30 days", seen["block"])
        self.assertEqual(seen["used"], "document")
        self.assertEqual(outcomes[1].depends_on, ["document"])

    async def test_waves_run_in_order(self):
        order: list[str] = []

        async def slow(step, ctx):
            order.append("start:" + step.agent)
            await asyncio.sleep(0.05)
            order.append("end:" + step.agent)
            return AgentOutcome(agent=step.agent, status="ok", answer=step.agent)

        plan = Plan(
            intent="x",
            summary="s",
            steps=[
                PlanStep(agent="search", task="t", reason="r"),
                PlanStep(agent="data", task="t", reason="r"),
                PlanStep(agent="general", task="t", reason="r", depends_on=["search", "data"]),
            ],
        )
        runners = {name: slow for name in ("search", "data", "general")}
        with mock.patch.dict(agents_module.AGENT_RUNNERS, runners):
            await run_plan(plan, AgentContext("m", [], None, None, settings_for()))
        self.assertLess(order.index("end:search"), order.index("start:general"))
        self.assertLess(order.index("end:data"), order.index("start:general"))
        self.assertLess(order.index("start:search"), order.index("end:data"))


class TraceTests(unittest.TestCase):
    def test_events_are_ordered_and_timed(self):
        trace = RunTrace()
        trace.add("run", "manager", "first")
        trace.add("agent", "search", "second", latency_ms=12)
        data = trace.to_list()
        self.assertEqual([event["seq"] for event in data], [1, 2])
        self.assertEqual(data[1]["detail"]["latency_ms"], 12)
        self.assertIn("offset_ms", data[0])

    def test_prompts_are_recorded_verbatim(self):
        trace = RunTrace()
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        trace.add_prompt("agent", "general", "prompt", messages)
        self.assertEqual(trace.to_list()[0]["detail"]["messages"], messages)

    def test_clipping_only_happens_on_serialisation(self):
        trace = RunTrace()
        trace.add("agent", "general", "big", content="x" * 1000)
        clipped = trace.to_list(limit=10)
        self.assertLess(len(clipped[0]["detail"]["content"]), 1000)
        self.assertIn("more chars", clipped[0]["detail"]["content"])
        self.assertEqual(len(trace.to_list()[0]["detail"]["content"]), 1000)

    def test_clip_event_walks_nested_structures(self):
        event = {"detail": {"messages": [{"role": "user", "content": "y" * 50}]}}
        clipped = clip_event(event, 5)["detail"]["messages"][0]["content"]
        self.assertTrue(clipped.startswith("yyyyy"))
        self.assertIn("45 more chars", clipped)

    def test_summary_lists_agents_and_tools(self):
        trace = RunTrace()
        trace.add("agent", "search", "task")
        trace.add("tool", "search", "call", kind="call", tool="web_search")
        self.assertEqual(trace.summary()["agents"], ["search"])
        self.assertEqual(trace.summary()["tool_calls"], ["web_search"])


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_skipped_for_a_single_agent(self):
        model = FakeModel()
        settings = settings_for(orchestrator_review=True)
        manager = Manager(settings=settings, model_chain=model, search_chain=FakeSearchChain())
        result = await manager.run("Summarise the attached policy.", attachments=[doc_file()])
        # document only → no review call
        self.assertFalse(result.review["asked"])
        self.assertIn("one agent", result.review["reason"])
        self.assertFalse(any("reviewing what came back" in m[0]["content"] for m in model.calls))

    async def test_review_says_nothing_missing(self):
        model = FakeModel()
        manager = Manager(
            settings=settings_for(orchestrator_review=True),
            model_chain=model,
            search_chain=FakeSearchChain(),
        )
        result = await manager.run(
            "Summarise the policy and compute the total revenue.",
            attachments=[doc_file(), csv_file()],
        )
        self.assertTrue(result.review["asked"])
        self.assertEqual(result.review["follow_up"], [])
        self.assertTrue(any("reviewing what came back" in m[0]["content"] for m in model.calls))

    async def test_review_can_ask_for_another_agent(self):
        review = {
            "note": "the numbers need a second look",
            "follow_up": [
                {"agent": "general", "task": "Explain the revenue trend.", "reason": "needs prose"}
            ],
        }
        model = FakeModel(review=review)
        manager = Manager(
            settings=settings_for(orchestrator_review=True),
            model_chain=model,
            search_chain=FakeSearchChain(),
        )
        result = await manager.run(
            "Summarise the policy and compute the total revenue.",
            attachments=[doc_file(), csv_file()],
        )
        self.assertTrue(result.review["follow_up"])
        self.assertEqual(result.review["follow_up"][0]["agent"], "general")
        agents = [outcome.agent for outcome in result.results]
        self.assertEqual(agents.count("general"), 1)
        # the follow-up agent ran in a later wave and saw the earlier results
        follow_up = result.results[-1]
        self.assertGreaterEqual(follow_up.wave, 2)
        self.assertTrue(follow_up.depends_on)
        prompts = [m[-1]["content"] for m in model.calls if "generalist" in m[0]["content"]]
        self.assertTrue(any("Findings from agents that ran before you" in text for text in prompts))

    async def test_review_can_be_disabled(self):
        model = FakeModel()
        manager = Manager(
            settings=settings_for(orchestrator_review=False),
            model_chain=model,
            search_chain=FakeSearchChain(),
        )
        result = await manager.run(
            "Summarise the policy and compute the total revenue.",
            attachments=[doc_file(), csv_file()],
        )
        self.assertFalse(result.review["asked"])
        self.assertIn("ORCHESTRATOR_REVIEW", result.review["reason"])

    def test_parse_review_filters_the_roster(self):
        raw = json.dumps(
            {"note": "n", "follow_up": [{"agent": "hacker", "task": "x"}, {"agent": "data", "task": "y"}]}
        )
        parsed = _parse_review(raw, ["data", "general"])
        self.assertEqual([item["agent"] for item in parsed["follow_up"]], ["data"])

    def test_parse_review_handles_noise(self):
        self.assertEqual(_parse_review("not json", ["data"])["follow_up"], [])
        self.assertEqual(_parse_review('{"follow_up": []}', ["data"])["follow_up"], [])


class TraceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_carries_a_full_trace(self):
        manager = Manager(
            settings=settings_for(orchestrator_planner="auto"),
            model_chain=FakeModel(),
            search_chain=FakeSearchChain(),
        )
        payload = (
            await manager.run(
                "Summarise the policy and compute the total revenue.",
                attachments=[doc_file(), csv_file()],
            )
        ).to_dict()
        orch = payload["orchestration"]
        self.assertTrue(orch["run_id"].startswith("run-"))
        titles = [event["title"] for event in orch["trace"]]
        self.assertIn("Request received", titles)
        self.assertIn("Planner prompt (writes each agent's brief)", titles)
        self.assertIn("Final synthesis prompt", titles)
        # agent prompts and replies are in there
        stages = {(event["actor"], event["stage"]) for event in orch["trace"]}
        self.assertIn(("document", "agent"), stages)
        self.assertIn(("manager", "synthesis"), stages)
        prompts = [
            event for event in orch["trace"] if event["detail"].get("messages")
        ]
        self.assertTrue(prompts)
        for prompt in prompts:
            self.assertIn("role", prompt["detail"]["messages"][0])

    async def test_trace_is_clipped_but_log_is_not(self):
        trace = RunTrace()
        trace.add("agent", "general", "big", content="z" * 500)
        self.assertIn("more chars", trace.to_list(limit=50)[0]["detail"]["content"])
        self.assertEqual(len(trace.to_list()[0]["detail"]["content"]), 500)

    async def test_agents_count_their_model_and_tool_calls(self):
        async def fake_search(step, ctx):
            ctx.emit("agent", "search", "Task received", task=step.task)
            ctx.emit("tool", "search", "Tool call: web_search", kind="call", tool="web_search")
            ctx.emit("tool", "search", "Tool result: web_search", kind="result", tool="web_search")
            return AgentOutcome(
                agent="search",
                status="ok",
                answer="found",
                model_calls=2,
                tool_calls=1,
                sources=["https://example.org"],
            )

        plan = Plan(intent="x", summary="s", steps=[PlanStep(agent="search", task="t", reason="r")])
        with mock.patch.dict(agents_module.AGENT_RUNNERS, {"search": fake_search}):
            outcomes = await run_plan(
                plan, AgentContext("m", [], None, None, settings_for(), trace=RunTrace())
            )
        self.assertEqual(outcomes[0].model_calls, 2)
        self.assertEqual(outcomes[0].tool_calls, 1)
        self.assertEqual(outcomes[0].sources, ["https://example.org"])


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, run_id="run-1"):
        return {
            "run_id": run_id,
            "at": "2026-10-31T10:00:00+00:00",
            "message": "summarise the file",
            "answer": "30 days",
            "status": "ok",
            "latency_ms": 120,
            "errors": [],
            "orchestration": {
                "plan": {"intent": "document-qa", "agents": ["document"], "strategy": "auto"},
                "synthesized": True,
                "parallel_ms": 40,
                "model_calls": 3,
                "trace": [{"seq": 1, "stage": "run", "actor": "manager", "title": "Request received"}],
            },
            "trace_summary": {"tool_calls": ["web_search"]},
        }

    def test_save_and_read_back(self):
        path = runlog.save_run(self.record(), log_dir=self.dir)
        self.assertTrue(path.is_file())
        self.assertTrue((Path(self.dir) / "orchestrator" / "runs.jsonl").exists())
        run = runlog.get_run("run-1", log_dir=self.dir)
        self.assertEqual(run["answer"], "30 days")
        self.assertEqual(len(run["orchestration"]["trace"]), 1)

    def test_list_newest_first(self):
        runlog.save_run(self.record("run-1"), log_dir=self.dir)
        runlog.save_run(self.record("run-2"), log_dir=self.dir)
        runs = runlog.list_runs(log_dir=self.dir)
        self.assertEqual([run["run_id"] for run in runs], ["run-2", "run-1"])
        self.assertEqual(runs[0]["intent"], "document-qa")
        self.assertEqual(runs[0]["tool_calls"], ["web_search"])

    def test_unknown_and_unsafe_ids(self):
        self.assertIsNone(runlog.get_run("missing", log_dir=self.dir))
        self.assertIsNone(runlog.get_run("../../etc/passwd", log_dir=self.dir))

    def test_list_on_empty_dir(self):
        self.assertEqual(runlog.list_runs(log_dir=self.dir), [])

    def test_corrupt_line_is_skipped(self):
        runlog.save_run(self.record("run-1"), log_dir=self.dir)
        path = Path(self.dir) / "orchestrator" / "runs.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        self.assertEqual(len(runlog.list_runs(log_dir=self.dir)), 1)

    async def test_manager_writes_the_run_log(self):
        with mock.patch.dict("os.environ", {"LOG_DIR": self.dir}):
            manager = Manager(
                settings=settings_for(orchestrator_log_runs=True),
                model_chain=FakeModel(),
                search_chain=FakeSearchChain(),
            )
            result = await manager.run("Explain closures.", attachments=[])
            self.assertTrue(result.log_path)
            stored = runlog.get_run(result.run_id, log_dir=self.dir)
        self.assertEqual(stored["run_id"], result.run_id)
        self.assertIn("orchestration", stored)
        self.assertTrue(stored["orchestration"]["trace"])
