"""Parallel execution of the plan.

Each selected agent runs concurrently — the slowest one decides the wall clock,
not the sum of all of them. Failures are contained: one agent dying never takes
the request down, it just arrives at the manager as ``status="failed"`` with the
error text.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app import logs
from app.orchestrator.agents import AGENT_RUNNERS, AgentContext, AgentOutcome
from app.orchestrator.planner import Plan, PlanStep


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
    log.info(
        "agent %s %s in %sms", outcome.agent, outcome.status, outcome.latency_ms
    )
    return outcome


async def run_plan(plan: Plan, ctx: AgentContext, *, settings: Any = None) -> list[AgentOutcome]:
    """Run every step concurrently; results keep the plan's order."""
    settings = settings or ctx.settings
    log = logs.get_logger("orchestrator")
    timeout = float(getattr(settings, "orchestrator_timeout", 120.0))
    parallel = max(int(getattr(settings, "orchestrator_max_parallel", 4)), 1)
    semaphore = asyncio.Semaphore(parallel)

    started = time.perf_counter()
    outcomes = await asyncio.gather(
        *(_run_one(step, ctx, semaphore, timeout, log) for step in plan.steps)
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    log.info(
        "plan %s ran %d agent(s) in %sms (serial estimate %sms)",
        plan.intent,
        len(outcomes),
        elapsed,
        sum(outcome.latency_ms for outcome in outcomes),
    )
    return list(outcomes)
