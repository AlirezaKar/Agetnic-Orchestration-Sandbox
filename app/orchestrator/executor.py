"""Parallel execution of the plan, one *wave* at a time.

Agents with no dependencies run together (wave 1). An agent that depends on
another waits for it, and receives that agent's result as input (wave 2, 3, …).
Inside a wave everything is concurrent — the slowest agent decides the wall
clock, not the sum of all of them.

Failures are contained: one agent dying never takes the request down, it just
arrives downstream as ``status="failed"`` with the error text.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app import logs
from app.orchestrator.agents import AGENT_RUNNERS, AgentContext, AgentOutcome
from app.orchestrator.planner import Plan, PlanStep


def schedule(plan: Plan) -> list[list[PlanStep]]:
    """Group steps into waves from their ``depends_on``; cycles go in the last wave."""
    remaining = list(plan.steps)
    done: set[str] = set()
    waves: list[list[PlanStep]] = []
    while remaining:
        ready = [
            step for step in remaining if all(dep in done for dep in step.depends_on)
        ]
        if not ready:  # cycle or a dependency on an agent that was never planned
            ready = list(remaining)
        wave = len(waves) + 1
        for step in ready:
            step.wave = wave
        waves.append(ready)
        for step in ready:
            done.add(step.agent)
            remaining.remove(step)
    return waves


async def _run_one(
    step: PlanStep,
    ctx: AgentContext,
    semaphore: asyncio.Semaphore,
    timeout: float,
    log: Any,
) -> AgentOutcome:
    runner = AGENT_RUNNERS.get(step.agent)
    started = time.perf_counter()
    if runner is None:
        return AgentOutcome(
            agent=step.agent,
            status="failed",
            task=step.task,
            reason=step.reason,
            error=f"unknown agent '{step.agent}'",
            wave=step.wave,
        )
    async with semaphore:
        try:
            outcome = await asyncio.wait_for(runner(step, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            outcome = AgentOutcome(
                agent=step.agent,
                status="failed",
                task=step.task,
                reason=step.reason,
                error=f"timed out after {timeout:.0f}s",
                hint="Raise ORCHESTRATOR_TIMEOUT, or ask a narrower question.",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - one agent must not kill the run
            log.exception("agent %s crashed", step.agent)
            outcome = AgentOutcome(
                agent=step.agent,
                status="failed",
                task=step.task,
                reason=step.reason,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
    outcome.task = outcome.task or step.task
    outcome.reason = outcome.reason or step.reason
    outcome.wave = step.wave
    outcome.depends_on = list(step.depends_on)
    log.info(
        "agent %s %s in %sms (wave %s)", outcome.agent, outcome.status, outcome.latency_ms, step.wave
    )
    ctx.emit(
        "agent",
        outcome.agent,
        f"{outcome.label} finished: {outcome.status}",
        status=outcome.status,
        latency_ms=outcome.latency_ms,
        wave=step.wave,
        model_calls=outcome.model_calls,
        tool_calls=outcome.tool_calls,
        answer=outcome.answer,
        error=outcome.error,
        sources=outcome.sources,
        depends_on=outcome.depends_on,
    )
    return outcome


async def run_plan(
    plan: Plan,
    ctx: AgentContext,
    *,
    settings: Any = None,
    wave_offset: int = 0,
) -> list[AgentOutcome]:
    """Run the plan wave by wave; results keep the plan's order.

    ``wave_offset`` lets the manager run a follow-up plan *after* the waves that
    already happened, so the trace keeps a single, increasing wave numbering.
    """
    settings = settings or ctx.settings
    log = logs.get_logger("orchestrator")
    timeout = float(getattr(settings, "orchestrator_timeout", 120.0))
    parallel = max(int(getattr(settings, "orchestrator_max_parallel", 4)), 1)
    semaphore = asyncio.Semaphore(parallel)
    waves = schedule(plan)
    if wave_offset:
        for step in plan.steps:
            step.wave += wave_offset
    started = time.perf_counter()
    ordered: list[AgentOutcome] = []
    for index, wave in enumerate(waves, start=1 + wave_offset):
        ctx.emit(
            "run",
            "manager",
            f"Wave {index}: {len(wave)} agent(s) start"
            + (" in parallel" if len(wave) > 1 else ""),
            wave=index,
            agents=[step.agent for step in wave],
            with_input_from={
                step.agent: step.depends_on for step in wave if step.depends_on
            },
        )
        wave_outcomes = await asyncio.gather(
            *(_run_one(step, ctx, semaphore, timeout, log) for step in wave)
        )
        ordered.extend(wave_outcomes)
        for outcome in wave_outcomes:
            if outcome.status == "ok":
                ctx.upstream[outcome.agent] = outcome
        ctx.emit(
            "run",
            "manager",
            f"Wave {index} finished",
            wave=index,
            results={outcome.agent: outcome.status for outcome in wave_outcomes},
        )

    by_agent = {outcome.agent: outcome for outcome in ordered}
    results = [by_agent[step.agent] for step in plan.steps if step.agent in by_agent]
    elapsed = int((time.perf_counter() - started) * 1000)
    log.info(
        "plan %s ran %d agent(s) in %d wave(s) in %sms (serial estimate %sms)",
        plan.intent,
        len(results),
        len(waves),
        elapsed,
        sum(outcome.latency_ms for outcome in results),
    )
    return results
