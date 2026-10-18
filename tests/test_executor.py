"""
tests.test_executor — parallel wave scheduling & failure isolation.
"""

import asyncio
import time

from backend.agents.base import AgentContext
from backend.orchestration.executor import ParallelExecutor
from backend.orchestration.trace import TraceRecorder
from backend.schemas import (
    AgentName,
    AgentResult,
    RunTrace,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    new_id,
)


class RecordingAgent:
    """Fake agent that logs start/end order and can simulate failures."""

    def __init__(self, name: AgentName, delay: float = 0.05, fail: bool = False):
        self.name = name
        self.delay = delay
        self.fail = fail
        self.events: list[str] = []

    async def run(self, task, context):
        self.events.append(f"start:{task}")
        await asyncio.sleep(self.delay)
        self.events.append(f"end:{task}")
        if self.fail:
            return AgentResult(agent=self.name, success=False, error="boom")
        upstream = context.upstream_text()
        return AgentResult(
            agent=self.name,
            success=True,
            output=f"done:{task}" + (f" | upstream={upstream[:40]}" if upstream else ""),
        )


def _recorder() -> TraceRecorder:
    return TraceRecorder(RunTrace(run_id=new_id("run"), input="test"))


def _plan(*specs: TaskSpec) -> TaskPlan:
    return TaskPlan(analysis="test", tasks=list(specs))


async def test_independent_tasks_run_in_parallel():
    """Two independent tasks must overlap in time (one wave)."""
    a1 = RecordingAgent(AgentName.GENERAL, delay=0.12)
    a2 = RecordingAgent(AgentName.NUMBER, delay=0.12)
    plan = _plan(
        TaskSpec(id="t1", agent=AgentName.GENERAL, description="one"),
        TaskSpec(id="t2", agent=AgentName.NUMBER, description="two"),
    )
    executor = ParallelExecutor({AgentName.GENERAL: a1, AgentName.NUMBER: a2}, _recorder())

    started = time.perf_counter()
    results = await executor.execute(plan, base_context=AgentContext())
    elapsed = time.perf_counter() - started

    assert all(r.status == TaskStatus.COMPLETED for r in results)
    # Serial would take ~0.24s; parallel should finish near ~0.12s.
    assert elapsed < 0.22, f"tasks did not run in parallel (took {elapsed:.2f}s)"
    assert results[0].wave == 0 and results[1].wave == 0


async def test_dependent_tasks_get_upstream_results():
    shared_log: list[str] = []  # wall-clock order shared by both agents

    class LoggingAgent(RecordingAgent):
        async def run(self, task, context):
            shared_log.append(f"start:{task}")
            result = await super().run(task, context)
            shared_log.append(f"end:{task}")
            return result

    searcher = LoggingAgent(AgentName.WEB_SEARCH)
    analyst = LoggingAgent(AgentName.NUMBER)
    plan = _plan(
        TaskSpec(id="t1", agent=AgentName.WEB_SEARCH, description="search it"),
        TaskSpec(id="t2", agent=AgentName.NUMBER, description="analyze it", depends_on=["t1"]),
    )
    executor = ParallelExecutor(
        {AgentName.WEB_SEARCH: searcher, AgentName.NUMBER: analyst}, _recorder()
    )
    results = await executor.execute(plan, base_context=AgentContext())

    assert results[1].wave == 1  # second wave
    assert results[1].result.output.startswith("done:analyze it")
    assert "upstream=" in results[1].result.output  # dependency injected
    # Ordering: search must fully finish before analysis starts.
    assert shared_log.index("end:search it") < shared_log.index("start:analyze it")


async def test_failed_task_does_not_break_plan():
    good = RecordingAgent(AgentName.GENERAL)
    bad = RecordingAgent(AgentName.NUMBER, fail=True)
    plan = _plan(
        TaskSpec(id="t1", agent=AgentName.GENERAL, description="good"),
        TaskSpec(id="t2", agent=AgentName.NUMBER, description="bad"),
    )
    executor = ParallelExecutor({AgentName.GENERAL: good, AgentName.NUMBER: bad}, _recorder())
    results = await executor.execute(plan, base_context=AgentContext())

    by_id = {r.task_id: r for r in results}
    assert by_id["t1"].status == TaskStatus.COMPLETED
    assert by_id["t2"].status == TaskStatus.FAILED
    assert by_id["t2"].error == "boom"


async def test_every_task_appears_in_trace():
    recorder = _recorder()
    agent = RecordingAgent(AgentName.GENERAL)
    plan = _plan(TaskSpec(id="t1", agent=AgentName.GENERAL, description="hello"))
    executor = ParallelExecutor({AgentName.GENERAL: agent}, recorder)
    await executor.execute(plan, base_context=AgentContext())

    steps = recorder.trace.steps
    assert len(steps) == 1
    assert steps[0].id == "task_t1"
    assert steps[0].output == "done:hello"
    assert steps[0].status == TaskStatus.COMPLETED
