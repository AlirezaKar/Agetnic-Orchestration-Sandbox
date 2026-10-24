"""
backend.orchestration.manager
=============================

**Manager** — the root brain of the orchestration architecture.

This is the component that answers the core question: *"given any user
prompt, how is the goal reached via the correct agents?"*

Auto-mode flow
--------------
1. **Analyse**      — the Manager reasons about the goal (what is being
                      asked, which capabilities are needed, what "done"
                      looks like).  → ``manager_thinking`` trace step.
2. **Plan**         — the Planner compiles the goal into a dependency-
                      aware task list (which agents, in which order).
                      → ``plan`` trace step.
3. **Delegate**     — the parallel Executor runs the plan: independent
                      tasks concurrently (waves), dependent tasks after
                      their upstream results exist.
4. **Supervise**    — after execution the Manager audits the results; if
                      something failed or is clearly insufficient it
                      **re-plans** (bounded by ``MAX_REPLAN_ATTEMPTS``).
5. **Synthesise**   — all task results are merged into one coherent
                      answer.  → ``synthesis`` trace step.

Manual-mode flow
----------------
The user pinned a mode: the Manager skips planning and delegates the
whole prompt to that single agent — but still records the full trace so
manual runs can be evaluated with the same tooling.

Everything the Manager does (thinking, decisions, errors) lands in the
run-trace JSON via :class:`TraceRecorder`.
"""

from __future__ import annotations

import time
from typing import List, Optional

from ..agents.base import AgentContext, BaseAgent
from ..config import settings
from ..llm import BaseLLMClient, LLMMessage
from ..schemas import (
    AgentName,
    AgentResult,
    ChatRequest,
    ChatResponse,
    Mode,
    RunTrace,
    TaskPlan,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TraceStep,
    new_id,
    utc_now_iso,
)
from .executor import ParallelExecutor
from .planner import Planner
from .router import Router
from .session import session_store
from .trace import TraceRecorder

# ──────────────────────────────────────────────────────────────────
#  Prompts
# ──────────────────────────────────────────────────────────────────
MANAGER_THINK_PROMPT = """You are the Manager of a multi-agent orchestration system.

Before delegating, analyse the user's goal. Produce a short operational
analysis covering:
1. What the user actually wants (intent, outputs, constraints).
2. Which capabilities are required (general knowledge, web search,
   documents, quantitative analysis) and why.
3. How the work should be structured (what can run in parallel, what must
   wait) and what a successful final answer must contain.

Be concrete and operational — this analysis guides the Planner."""


class Manager:
    """
    Root orchestrator: analyse → plan → delegate → supervise → synthesise.

    Constructed with the LLM and the agent team so it is fully
    testable with the offline mock (no I/O besides the trace file).
    """

    def __init__(
        self,
        llm: BaseLLMClient,
        agents: dict[AgentName, BaseAgent],
    ) -> None:
        self._llm = llm
        self._agents = agents
        self._planner = Planner(llm)
        self._router = Router()

    # ══════════════════════════════════════════════════════════
    #  Public entry point
    # ══════════════════════════════════════════════════════════
    async def run(self, request: ChatRequest) -> ChatResponse:
        """Process one user turn end-to-end and return the API response."""
        started = time.perf_counter()
        trace = RunTrace(
            run_id=new_id("run"),
            session_id=request.session_id,
            mode_requested=request.mode,
            mode_used=request.mode,
            input=request.message,
            config=settings.redacted(),
        )
        recorder = TraceRecorder(trace)

        try:
            if request.mode == Mode.AUTO:
                output = await self._run_auto(request, trace, recorder)
            else:
                output = await self._run_manual(request, trace, recorder)
            trace.finish(output, success=True)
        except Exception as exc:  # noqa: BLE001 — a crash is still traced
            trace.finish("", success=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            trace.metrics["total_duration_ms"] = round(
                (time.perf_counter() - started) * 1000, 2
            )
            trace_file = recorder.save_relative()

        # Remember the turn for follow-up questions in this session.
        session_store.append(request.session_id, "user", request.message)
        session_store.append(request.session_id, "assistant", trace.output)

        return ChatResponse(
            run_id=trace.run_id,
            session_id=trace.session_id,
            mode_requested=trace.mode_requested,
            mode_used=trace.mode_used,
            input=trace.input,
            output=trace.output,
            thinking=trace.thinking,
            plan=trace.plan,
            steps=trace.steps,
            metrics=trace.metrics,
            trace_file=trace_file,
            success=trace.success,
            error=trace.error,
        )

    # ══════════════════════════════════════════════════════════
    #  Auto mode — the full orchestration architecture
    # ══════════════════════════════════════════════════════════
    async def _run_auto(
        self,
        request: ChatRequest,
        trace: RunTrace,
        recorder: TraceRecorder,
    ) -> str:
        goal = request.message

        # ── 1. Manager analysis ────────────────────────────────
        analysis = await self._analyse(goal)
        recorder.add_thinking(analysis)

        # ── 2. Planning ────────────────────────────────────────
        plan, results = await self._plan_and_execute(
            goal, analysis, request, trace, recorder
        )

        # ── 3. Supervision: bounded re-planning on failure ─────
        attempt = 0
        while self._needs_replan(results) and attempt < settings.max_replan_attempts:
            attempt += 1
            recorder.replace_steps_for_replan()
            recorder.add_thinking(
                f"[Manager decision] {self._replan_reason(results)} "
                f"Re-planning (attempt {attempt}/{settings.max_replan_attempts})."
            )
            failure_note = self._replan_reason(results)
            plan, results = await self._plan_and_execute(
                goal,
                f"{analysis}\n\nPREVIOUS ATTEMPT ISSUE: {failure_note}",
                request,
                trace,
                recorder,
                replan=True,
            )

        # ── 4. Synthesis ───────────────────────────────────────
        step = TraceStep(
            id="synthesis",
            type="synthesis",
            label="Manager synthesis",
            status=TaskStatus.RUNNING,
            started_at=utc_now_iso(),
        )
        synth_started = time.perf_counter()
        output = await self._synthesize(goal, results)
        step.output = output
        step.status = TaskStatus.COMPLETED
        step.finished_at = utc_now_iso()
        step.duration_ms = (time.perf_counter() - synth_started) * 1000
        step.data["agents_used"] = sorted({r.agent.value for r in results if r.result})
        recorder.add_step(step)

        self._fill_metrics(trace, results)
        return output

    # ══════════════════════════════════════════════════════════
    #  Manual mode — pinned single-agent hand-off
    # ══════════════════════════════════════════════════════════
    async def _run_manual(
        self,
        request: ChatRequest,
        trace: RunTrace,
        recorder: TraceRecorder,
    ) -> str:
        agent = self._router.agent_for_mode(request.mode)
        assert agent is not None  # AUTO never reaches this branch

        recorder.add_thinking(
            f"[Manager] Manual mode '{request.mode.value}' selected by the user → "
            f"delegating the full prompt to the {agent.value} agent "
            f"(single-task plan, full trace)."
        )

        # Even manual runs go through Plan → Execute so the trace format
        # is identical for every run (easier evaluation tooling).
        plan = TaskPlan(
            analysis=f"Manual mode: user pinned '{request.mode.value}'.",
            tasks=[
                TaskSpec(
                    id="t1",
                    agent=agent,
                    description=request.message,
                    depends_on=[],
                    reason=f"User selected mode '{request.mode.value}'.",
                )
            ],
        )
        recorder.set_plan(plan)

        executor = ParallelExecutor(self._agents, recorder)
        results = await executor.execute(plan, base_context=self._context_for(request))
        recorder.add_thinking(self._result_audit(results))
        self._fill_metrics(trace, results)

        # Single-agent runs return the agent's output directly; the
        # Manager only quotes it (no information should be lost in
        # "synthesis" for manual mode).
        for result in results:
            if result.result and result.result.success:
                return result.result.output
        return (
            "The selected agent failed to produce a result. "
            f"Details: {results[0].error if results else 'unknown error'}"
        )

    # ══════════════════════════════════════════════════════════
    #  Building blocks
    # ══════════════════════════════════════════════════════════
    async def _analyse(self, goal: str) -> str:
        """Manager thinking step: operational analysis of the goal."""
        response = await self._llm.complete(
            [
                LLMMessage("system", MANAGER_THINK_PROMPT),
                LLMMessage("user", f"## USER GOAL\n{goal}"),
            ]
        )
        return response.content

    async def _plan_and_execute(
        self,
        goal: str,
        analysis: str,
        request: ChatRequest,
        trace: RunTrace,
        recorder: TraceRecorder,
        *,
        replan: bool = False,
    ) -> tuple[TaskPlan, List[TaskResult]]:
        """One Planning + Execution cycle (also used for re-plans)."""
        plan, plan_response = await self._planner.create_plan(
            goal, analysis, request.allowed_agents
        )
        recorder.set_plan(plan)

        plan_step = TraceStep(
            id=f"plan_{'re' if replan else ''}{trace.replan_count}",
            type="plan",
            label="Planner task plan",
            status=TaskStatus.COMPLETED,
            thinking=plan_response.content,
            output=plan.analysis,
            started_at=utc_now_iso(),
            finished_at=utc_now_iso(),
            data={
                "tasks": [t.model_dump(mode="json") for t in plan.tasks],
                "waves": [[t.id for t in wave] for wave in plan.waves()],
                "replan": replan,
            },
        )
        recorder.add_step(plan_step)

        executor = ParallelExecutor(self._agents, recorder)
        results = await executor.execute(plan, base_context=self._context_for(request))
        recorder.add_thinking(self._result_audit(results))
        return plan, results

    def _context_for(self, request: ChatRequest) -> AgentContext:
        """Shared agent context for one user turn."""
        return AgentContext(
            history=session_store.history(request.session_id),
            shared={"session_id": request.session_id, "mode": request.mode.value},
        )

    async def _synthesize(self, goal: str, results: List[TaskResult]) -> str:
        """
        Merge all task results into one final answer.

        If the LLM synthesis is somehow empty, fall back to a transparent
        concatenation of the successful task outputs.
        """
        blocks = []
        for result in results:
            status = result.status.value
            body = result.result.output if result.result else (result.error or "")
            blocks.append(
                f"### task {result.task_id} ({result.agent.value}) [{status}]\n"
                f"Output: {body}"
            )
        results_text = "\n\n".join(blocks) if blocks else "(no tasks were executed)"

        prompt = (
            f"## USER GOAL\n{goal}\n\n"
            f"## TASK RESULTS\n{results_text}\n\n"
            f"Combine the task results into one coherent, complete answer to "
            f"the user's goal. Keep every important finding; do not mention "
            f"internal task ids."
        )
        response = await self._llm.complete(
            [
                LLMMessage(
                    "system",
                    "You are the Manager of a multi-agent orchestration "
                    "system. You merge specialist task results into the final "
                    "answer for the user.",
                ),
                LLMMessage("user", prompt),
            ]
        )
        text = (response.content or "").strip()
        if text:
            return text

        # Deterministic fallback: transparent concatenation.
        return "\n\n".join(
            f"[{r.agent.value}]\n{r.result.output}"
            for r in results
            if r.result and r.result.output
        )

    # ══════════════════════════════════════════════════════════
    #  Supervision helpers
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _needs_replan(results: List[TaskResult]) -> bool:
        """True when something failed and the results deserve a retry."""
        return any(r.status == TaskStatus.FAILED for r in results)

    @staticmethod
    def _replan_reason(results: List[TaskResult]) -> str:
        failed = [f"{r.task_id} ({r.agent.value}): {r.error}" for r in results if r.status == TaskStatus.FAILED]
        return "Failed tasks → " + "; ".join(failed)

    @staticmethod
    def _result_audit(results: List[TaskResult]) -> str:
        """Short Manager audit of execution, stored as thinking."""
        parts = []
        for r in results:
            mark = "✓" if r.status == TaskStatus.COMPLETED else "✗"
            parts.append(f"{mark} {r.task_id}/{r.agent.value} [{r.status.value}, {r.duration_ms:.0f}ms]")
        return "[Manager audit] Execution finished:\n" + "\n".join(parts)

    @staticmethod
    def _fill_metrics(trace: RunTrace, results: List[TaskResult]) -> None:
        """Populate the metrics block of the trace."""
        agents_used = sorted({r.agent.value for r in results})
        trace.metrics.update(
            {
                "tasks_total": len(results),
                "tasks_completed": sum(1 for r in results if r.status == TaskStatus.COMPLETED),
                "tasks_failed": sum(1 for r in results if r.status == TaskStatus.FAILED),
                "agents_used": agents_used,
                "replan_count": trace.replan_count,
            }
        )
