"""
backend.orchestration.executor
==============================

**Parallel executor** — runs a :class:`TaskPlan` as fast as its
dependencies allow.

Scheduling model
----------------
The plan is grouped into *waves* (see :meth:`TaskPlan.waves`):

* every task inside one wave is independent → run concurrently
* wave N+1 starts only after wave N finished
* a semaphore caps real concurrency at ``MAX_PARALLEL_TASKS``

Each task gets its own timeout (``TASK_TIMEOUT_SECONDS``); a failing task
never aborts the plan — it is recorded as ``failed`` and the Manager
decides whether to re-plan or synthesise from what succeeded.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List

from ..agents.base import AgentContext, BaseAgent
from ..config import settings
from ..schemas import (
    AgentName,
    AgentResult,
    TaskPlan,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TraceStep,
    utc_now_iso,
)
from .trace import TraceRecorder


class ParallelExecutor:
    """Executes task plans with per-wave concurrency and full tracing."""

    def __init__(
        self,
        agents: Dict[AgentName, BaseAgent],
        recorder: TraceRecorder,
    ) -> None:
        self._agents = agents
        self._recorder = recorder

    async def execute(
        self,
        plan: TaskPlan,
        *,
        base_context: AgentContext,
    ) -> List[TaskResult]:
        """
        Run every task of ``plan``.  Returns one :class:`TaskResult` per
        task, in plan order, and mirrors each one into the trace timeline.
        """
        waves = plan.waves()  # raises on cyclic deps — caller handles
        semaphore = asyncio.Semaphore(max(1, settings.max_parallel_tasks))
        # Completed results feed dependent tasks via the context.
        completed: Dict[str, AgentResult] = dict(base_context.upstream_results)

        all_results: List[TaskResult] = []
        for wave_index, wave in enumerate(waves):
            wave_tasks = [
                self._run_task(
                    spec,
                    wave_index=wave_index,
                    semaphore=semaphore,
                    base_context=base_context,
                    completed=completed,
                )
                for spec in wave
            ]
            wave_results = await asyncio.gather(*wave_tasks)
            all_results.extend(wave_results)
            for spec, result in zip(wave, wave_results):
                if result.result is not None and result.result.success:
                    completed[spec.id] = result.result

        return all_results

    # ── single task ─────────────────────────────────────────────
    async def _run_task(
        self,
        spec: TaskSpec,
        *,
        wave_index: int,
        semaphore: asyncio.Semaphore,
        base_context: AgentContext,
        completed: Dict[str, AgentResult],
    ) -> TaskResult:
        result = TaskResult(
            task_id=spec.id,
            agent=spec.agent,
            status=TaskStatus.RUNNING,
            input=spec.description,
            wave=wave_index,
            started_at=utc_now_iso(),
        )

        # Timeline step (status updated in place after execution).
        step = TraceStep(
            id=f"task_{spec.id}",
            type="agent_task",
            label=f"{spec.agent.value} · {spec.id}",
            status=TaskStatus.RUNNING,
            input=spec.description,
            started_at=result.started_at,
            data={
                "agent": spec.agent.value,
                "wave": wave_index,
                "depends_on": spec.depends_on,
                "reason": spec.reason,
            },
        )
        self._recorder.add_step(step)

        async with semaphore:
            started = time.perf_counter()
            try:
                # Only this task's declared dependencies are injected.
                context = AgentContext(
                    history=base_context.history,
                    upstream_results={
                        dep: completed[dep]
                        for dep in spec.depends_on
                        if dep in completed
                    },
                    shared=base_context.shared,
                )
                agent = self._agents[spec.agent]
                agent_result = await asyncio.wait_for(
                    agent.run(spec.description, context),
                    timeout=settings.task_timeout_seconds,
                )
                result.result = agent_result
                result.status = (
                    TaskStatus.COMPLETED if agent_result.success else TaskStatus.FAILED
                )
                result.error = agent_result.error
                step.thinking = agent_result.thinking
                step.output = agent_result.output
                step.data.update(agent_result.data)
                step.error = agent_result.error
            except asyncio.TimeoutError:
                result.status = TaskStatus.FAILED
                result.error = (
                    f"task timed out after {settings.task_timeout_seconds:.0f}s"
                )
                step.error = result.error
            except Exception as exc:  # noqa: BLE001 — recorded, never fatal
                result.status = TaskStatus.FAILED
                result.error = f"{type(exc).__name__}: {exc}"
                step.error = result.error
            finally:
                elapsed = (time.perf_counter() - started) * 1000
                result.duration_ms = elapsed
                result.finished_at = utc_now_iso()
                step.finished_at = result.finished_at
                step.duration_ms = elapsed
                step.status = result.status

        return result
