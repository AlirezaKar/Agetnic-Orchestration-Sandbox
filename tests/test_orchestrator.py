"""Orchestrator: planner routing, parallel executor, manager synthesis."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.config import Settings
from app.llm.base import ModelResponse
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
from app.orchestrator.executor import run_plan
from app.orchestrator.files import extract
from app.orchestrator.manager import Manager, deterministic_answer
from app.orchestrator.planner import (
    AGENTS,
    Plan,
    PlanStep,
    _parse_llm_plan,
    heuristic_plan,
    make_plan,
)
from app.search.base import SearchResponse, SearchResult


def settings_for(tmp_dir: str = "/tmp/agetnic-tests", **overrides) -> Settings:
    base = dict(
        _env_file=None,
        orchestrator_planner="heuristic",
        orchestrator_synthesize=True,
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

    def __init__(self, content: str = "answer", fail: bool = False):
        self.content = content
        self.fail = fail
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("provider exploded")
        system = messages[0]["content"] if messages else ""
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
        settings = settings_for()
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
        self.assertEqual(result.plan.strategy, "heuristic")
        # one call per agent + one synthesis, not a loop per agent
        self.assertEqual(len(model.calls), len(result.results) + 1)

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
