"""The orchestrator as a service: the boundary, the ports, and the wiring.

Two things are being protected here:

1. **The boundary.** Nothing inside ``app/orchestrator/`` may import another part
   of this repository, except ``host.py`` — the single seam. That is what makes
   the package portable to another project with a similar architecture.
2. **The contract.** The whole pipeline must run against *foreign* objects: a
   settings dataclass that is not ``app.config.Settings``, a model chain and a
   search chain with only the documented methods, and an injected agent loop.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.orchestrator import (
    AGENTS,
    AgentContext,
    OrchestrationRequest,
    OrchestratorService,
    PlanStep,
    RunTrace,
)
from app.orchestrator import host
from app.orchestrator.agents import run_search
from app.orchestrator.manager import Manager
from app.llm.base import ModelResponse
from app.search.base import SearchResponse, SearchResult

PACKAGE = Path(__file__).resolve().parents[1] / "app" / "orchestrator"


# ─────────────────────────────────────────────────────────── foreign doubles
@dataclass
class ForeignSettings:
    """A settings object that knows nothing about this repository."""

    orchestrator_enabled: bool = True
    orchestrator_planner: str = "heuristic"
    orchestrator_review: bool = False
    orchestrator_synthesize: bool = True
    orchestrator_agents: str = ""
    orchestrator_roster: list[str] = field(default_factory=lambda: list(AGENTS))
    orchestrator_context_chars: int = 4000
    orchestrator_trace_chars: int = 4000
    orchestrator_timeout: float = 5.0
    orchestrator_max_parallel: int = 4
    orchestrator_log_runs: bool = False
    llm_temperature: float = 0.2
    llm_max_tokens: int = 400
    agent_max_steps: int = 3
    agent_max_search_results: int = 3
    files_context_chars: int = 2000


class ForeignModel:
    """A model chain with exactly one method — everything the port asks for."""

    def __init__(self, answer: str = "A short answer.") -> None:
        self.answer = answer
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        system = messages[0]["content"] if messages else ""
        if "manager of a small agent team" in system:
            return ModelResponse(content=self.answer, provider="foreign", model="m-1")
        return ModelResponse(content="agent " + str(len(self.calls)), provider="foreign", model="m-1")


class ForeignSearch:
    async def search(self, query, limit=5):
        return SearchResponse(
            query=query,
            provider="foreign-search",
            results=[SearchResult(title="R", url="https://example.org/r", snippet="s")],
            latency_ms=1,
        )


class FakeLoop:
    """What an injected agent loop returns (the host's WebSearchAgent shape)."""

    def __init__(self, answer: str = "loop answer") -> None:
        self.answer = answer
        self.runs: list[str] = []

    async def run(self, message, *, history=None, allow_search=True, max_steps=None):
        self.runs.append(message)
        return SimpleNamespace(
            answer=self.answer,
            sources=["https://example.org/loop"],
            steps=2,
            provider="loop",
            model="loop-1",
            trace=[],
            search_provider="foreign-search",
        )


class FakeLoopFactory:
    """The port: called as factory(model_chain, search_chain, settings, observer=…)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.loop = FakeLoop()

    def __call__(self, model_chain, search_chain, settings, observer=None):
        self.calls.append(
            {"models": model_chain, "search": search_chain, "settings": settings, "observer": observer}
        )
        return self.loop


# ─────────────────────────────────────────────────────────────── 1. boundary
class BoundaryTests(unittest.TestCase):
    """``app/orchestrator/host.py`` is the only way out of the package."""

    def modules(self):
        return sorted(PACKAGE.glob("*.py"))

    def imports_of(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.append(node.module)
            elif isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
        return found

    def test_the_package_is_where_we_think_it_is(self):
        self.assertTrue((PACKAGE / "service.py").is_file())
        self.assertTrue((PACKAGE / "contracts.py").is_file())
        self.assertTrue((PACKAGE / "host.py").is_file())

    def test_only_host_py_reaches_into_the_application(self):
        for path in self.modules():
            for name in self.imports_of(path):
                if not name.startswith("app"):
                    continue
                if name.startswith("app.orchestrator"):
                    continue
                self.assertEqual(
                    path.name,
                    "host.py",
                    f"{path.name} imports {name}: give it a port in contracts.py instead",
                )

    def test_the_host_seam_only_knows_its_four_services(self):
        allowed = {"app", "app.logs", "app.agent", "app.config"}
        used = {
            name
            for name in self.imports_of(PACKAGE / "host.py")
            if name.startswith("app")
        }
        self.assertTrue(used.issubset(allowed), f"host.py imports {used - allowed}")

    def test_no_module_hides_an_import_at_runtime(self):
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                self.assertNotIn(
                    name,
                    {"__import__", "import_module"},
                    f"{path.name} imports at runtime — the boundary must stay visible",
                )

    def test_contracts_import_nothing_but_the_standard_library(self):
        for name in self.imports_of(PACKAGE / "contracts.py"):
            self.assertFalse(name.startswith("app"), f"contracts.py imports {name}")

    def test_the_package_is_listed_as_a_service(self):
        # the sibling services stay where they are; the orchestrator sits beside them
        root = PACKAGE.parent
        for sibling in ("llm", "search", "memory.py", "agent.py", "main.py"):
            self.assertTrue((root / sibling).exists(), sibling)


# ──────────────────────────────────────────────────────── 2. the service run
class ServiceTests(unittest.TestCase):
    def service(self, **kwargs) -> OrchestratorService:
        settings = kwargs.pop("settings", None) or ForeignSettings()
        return OrchestratorService(
            settings,
            models=kwargs.pop("models", None) or ForeignModel(),
            search=kwargs.pop("search", None) or ForeignSearch(),
            **kwargs,
        )

    def test_a_full_run_with_objects_from_another_project(self):
        import asyncio

        service = self.service()
        result = asyncio.run(service.run(OrchestrationRequest(message="Explain retrieval augmented generation.")))
        self.assertTrue(result.answer)
        self.assertEqual(result.plan.strategy, "heuristic")
        self.assertIn("general", result.plan.agents)
        self.assertTrue(result.run_id.startswith("run-"))
        self.assertTrue(result.trace.events)
        stages = [event["stage"] for event in result.trace.to_list()]
        self.assertIn("run", stages)
        self.assertIn("planner", stages)
        self.assertIn("done", stages)

    def test_describe_is_the_shape_the_ui_reads(self):
        described = self.service().describe()
        for key in (
            "agents", "roster", "planner", "review", "synthesize", "timeout_s",
            "parallel", "trace", "enabled", "all_agents", "model_ready", "search_ready",
        ):
            self.assertIn(key, described)
        self.assertTrue(described["model_ready"])
        self.assertTrue(described["search_ready"])
        self.assertEqual(described["planner"], "heuristic")

    def test_payload_clips_the_trace_for_the_browser(self):
        import asyncio

        settings = ForeignSettings(orchestrator_trace_chars=60)
        model = ForeignModel(answer="x" * 400)
        service = self.service(settings=settings, models=model)
        result = asyncio.run(service.run(OrchestrationRequest(message="Say something long.")))
        payload = service.payload(result)
        self.assertEqual(payload["answer"], "x" * 400, "the answer itself is not clipped")
        self.assertIn("more chars", json.dumps(payload["orchestration"]["trace"]))
        unclipped = service.payload(result, trace_limit=None)
        self.assertNotIn("more chars", json.dumps(unclipped["orchestration"]["trace"]))

    def test_the_run_log_directory_is_injectable(self):
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            settings = ForeignSettings(orchestrator_log_runs=True)
            service = self.service(settings=settings, log_dir=tmp)
            result = asyncio.run(service.run(OrchestrationRequest(message="Where is the log?")))
            self.assertTrue((Path(tmp) / "orchestrator" / "runs.jsonl").is_file())
            self.assertTrue(result.log_path.endswith(".json"))
            self.assertTrue(Path(result.log_path).is_file())

    def test_the_run_log_is_off_by_default_and_stays_off(self):
        import asyncio

        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(log_dir=tmp)
            result = asyncio.run(service.run(OrchestrationRequest(message="No log please.")))
            self.assertEqual(result.log_path, "")
            self.assertFalse((Path(tmp) / "orchestrator" / "runs.jsonl").exists())

    def test_boundary_reports_what_is_wired_in(self):
        boundary = self.service().boundary()
        self.assertEqual(boundary["settings"], "ForeignSettings")
        self.assertEqual(boundary["models"], "ForeignModel")
        self.assertEqual(boundary["search"], "ForeignSearch")
        self.assertTrue(boundary["agent_loop"], "the default agent loop comes from the host")
        self.assertIn("log_dir", boundary)

    def test_run_rejects_the_wrong_request_type(self):
        import asyncio

        with self.assertRaises(TypeError):
            asyncio.run(self.service().run({"message": "a dict is not a request"}))

    def test_the_request_can_be_built_from_an_http_payload(self):
        payload = SimpleNamespace(
            message="hello",
            history=[{"role": "user", "content": "earlier"}],
            agents=["general"],
            planner="heuristic",
            synthesize=False,
            review=True,
        )
        request = OrchestrationRequest.from_payload(payload)
        self.assertEqual(request.message, "hello")
        self.assertEqual(request.agents, ["general"])
        self.assertFalse(request.synthesize)
        self.assertTrue(request.review)
        self.assertEqual(request.attachments, [])


# ─────────────────────────────────────────────────── 3. injected collaborators
class InjectionTests(unittest.TestCase):
    def test_the_agent_loop_can_be_replaced(self):
        import asyncio

        factory = FakeLoopFactory()
        service = OrchestratorService(
            ForeignSettings(), models=ForeignModel(), search=ForeignSearch(), agent_loop=factory
        )
        result = asyncio.run(service.run(OrchestrationRequest(message="Search the web for the newest Python release.")))

        self.assertEqual(len(factory.calls), 1, "the search agent must use the injected loop")
        call = factory.calls[0]
        self.assertIsInstance(call["settings"], ForeignSettings)
        self.assertIsNotNone(call["observer"], "the loop must be able to feed the trace")
        search_outcome = next((item for item in result.results if item.agent == "search"), None)
        self.assertIsNotNone(search_outcome, f"expected a search agent in {[r.agent for r in result.results]}")
        self.assertEqual(search_outcome.answer, "loop answer")
        self.assertEqual(search_outcome.sources, ["https://example.org/loop"])

    def test_a_context_without_a_loop_falls_back_to_the_host_hook(self):
        import asyncio

        trace = RunTrace()
        ctx = AgentContext(
            message="find it",
            attachments=[],
            model_chain=ForeignModel(),
            search_chain=ForeignSearch(),
            settings=ForeignSettings(),
            trace=trace,
        )
        outcome = asyncio.run(run_search(PlanStep(agent="search", task="find it", reason="fresh"), ctx))
        self.assertEqual(outcome.status, "ok")
        self.assertTrue(outcome.answer)
        # the host's own loop was used, and it fed the trace like before
        self.assertTrue(any("Prompt sent to the model" in e["title"] for e in trace.to_list()))

    def test_the_manager_accepts_the_same_ports_directly(self):
        import asyncio

        factory = FakeLoopFactory()
        manager = Manager(
            settings=ForeignSettings(),
            model_chain=ForeignModel(),
            search_chain=ForeignSearch(),
            agent_loop=factory,
        )
        result = asyncio.run(manager.run("Search for the newest Python release."))
        self.assertTrue(result.answer)
        self.assertEqual(len(factory.calls), 1)

    def test_host_seam_returns_this_repository_wiring(self):
        # the defaults are still this app, which is why behaviour did not change
        self.assertTrue(callable(host.get_logger))
        self.assertTrue(callable(host.log_dir))
        self.assertEqual(host.agent_loop().__name__, "WebSearchAgent")
        self.assertTrue(hasattr(host.default_settings(), "orchestrator_roster"))


if "__main__" == __name__:  # pragma: no cover
    unittest.main()
