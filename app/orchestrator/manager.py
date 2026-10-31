"""Manager: plan → delegate (in waves) → review → write the answer.

The manager is deliberately thin, and every decision it takes is written to the
run trace:

1. **classify** — the request, the files, the roster (rules).
2. **plan** — ``ORCHESTRATOR_PLANNER``: rules select the agents, and with the
   default ``auto`` one small completion writes each agent's brief and the
   dependency graph (``heuristic`` writes them by rule, ``llm`` asks the model
   for everything).
3. **delegate** — agents run in parallel waves; an agent that depends on another
   receives that agent's result as input.
4. **review** — with more than one agent, the manager may ask for one follow-up
   agent task (``ORCHESTRATOR_REVIEW=false`` turns this off).
5. **wrap up** — one completion writes the final answer from the reports
   (``ORCHESTRATOR_SYNTHESIZE=false`` merges them deterministically instead).

Everything above is recorded, clipped for the browser and stored unclipped in
``logs/orchestrator/``.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app import logs
from app.config import Settings, get_settings
from app.orchestrator import runlog
from app.orchestrator.agents import AgentContext, AgentOutcome, catalogue
from app.orchestrator.executor import run_plan, schedule
from app.orchestrator.files import Attachment
from app.orchestrator.planner import AGENTS, Plan, PlanStep, make_plan
from app.orchestrator.trace import RunTrace

MANAGER_PROMPT = (
    "You are the manager of a small agent team. Your specialists have reported back; "
    "write the single answer the user asked for.\n"
    "Rules:\n"
    "- Answer the request directly, in the language the user used.\n"
    "- Use the specialists' findings; do not repeat their internal instructions.\n"
    "- Attribute claims: keep URLs for web findings, quote files by name for documents, "
    "and keep the computed figures exact.\n"
    "- If an agent failed, say what is missing in one sentence at the end instead of "
    "inventing the missing part.\n"
    "- Be compact: a short answer with structure beats a long report."
)

REVIEW_PROMPT = (
    "You are the manager of a small agent team, reviewing what came back before you "
    "answer the user.\n"
    "Decide whether one more piece of work is needed to answer well — for example an "
    "agent result that another agent should analyse, a tool result that needs checking, "
    "or a gap none of the agents covered.\n"
    "Be strict: only ask for work that clearly improves the answer. Never repeat a task "
    "that already succeeded.\n"
    'Answer with JSON only: {"note": "one sentence", "follow_up": [{"agent": "data", '
    '"task": "…", "reason": "…"}]} — use an empty list when nothing is needed.'
)


@dataclass
class OrchestrationResult:
    answer: str
    plan: Plan
    results: list[AgentOutcome]
    sources: list[str] = field(default_factory=list)
    strategy: str = "auto"
    synthesized: bool = False
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    parallel_ms: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    run_id: str = ""
    trace: RunTrace | None = None
    waves: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    model_calls: int = 0
    log_path: str = ""
    started_at: str = ""
    message: str = ""

    def to_dict(self, *, trace_limit: int | None = 4000) -> dict[str, Any]:
        orchestration = {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "synthesized": self.synthesized,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "parallel_ms": self.parallel_ms,
            "model_calls": self.model_calls,
            "files": self.files,
            "notes": self.notes,
            "plan": self.plan.to_dict(),
            "waves": self.waves,
            "review": self.review,
            "agents": [outcome.to_dict() for outcome in self.results],
            "trace": self.trace.to_list(limit=trace_limit) if self.trace else [],
            "trace_summary": self.trace.summary() if self.trace else {},
            "log_path": self.log_path,
        }
        return {
            "answer": self.answer,
            "mode": "auto",
            "run_id": self.run_id,
            "message": self.message,
            "orchestration": orchestration,
            "sources": self.sources,
            "errors": self.errors,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }

    def record(self) -> dict[str, Any]:
        """The unclipped record written to ``logs/orchestrator/``."""
        payload = self.to_dict(trace_limit=None)
        payload.update(
            {
                "at": self.started_at or _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "status": "ok" if self.answer else "empty",
            }
        )
        return payload


def _collect_sources(results: list[AgentOutcome]) -> list[str]:
    seen: list[str] = []
    for outcome in results:
        for url in outcome.sources:
            if url and url not in seen:
                seen.append(url)
    return seen


def _files_block(attachments: list[Attachment]) -> str:
    if not attachments:
        return "(none)"
    return "\n".join(
        f"- {item.name} ({item.kind}, {item.chars} chars"
        + (f", {len(item.rows) - 1} rows" if item.rows else "")
        + f"): {item.preview(160)}"
        for item in attachments
    )


def _results_block(results: list[AgentOutcome], limit: int) -> str:
    blocks: list[str] = []
    for outcome in results:
        head = f"### {outcome.label} ({outcome.agent}) — {outcome.status}, {outcome.latency_ms} ms"
        if outcome.status == "failed":
            body = f"FAILED: {outcome.error}" + (f" ({outcome.hint})" if outcome.hint else "")
        elif outcome.status == "skipped":
            body = f"SKIPPED: {outcome.detail or 'not applicable'}"
        else:
            body = outcome.answer.strip() or "(empty answer)"
            if outcome.agent == "data" and outcome.data.get("computed"):
                body += "\n\ncomputed figures:\n" + outcome.data["computed"]
            if outcome.sources:
                body += "\n\nsources: " + ", ".join(outcome.sources[:6])
        blocks.append(f"{head}\n{body[:limit]}")
    return "\n\n".join(blocks)


def deterministic_answer(message: str, results: list[AgentOutcome]) -> str:
    """Merge without a model: every usable agent answer, labelled."""
    parts = []
    for outcome in results:
        if outcome.status != "ok" or not outcome.answer.strip():
            continue
        parts.append(f"**{outcome.label}**\n\n{outcome.answer.strip()}")
    failed = [outcome.label for outcome in results if outcome.status == "failed"]
    if not parts:
        return (
            "No agent could answer this request.\n\n"
            + "\n".join(f"- {outcome.label}: {outcome.error or 'no output'}" for outcome in results)
        )
    body = "\n\n".join(parts)
    if failed:
        body += "\n\n_Unavailable: " + ", ".join(failed) + "._"
    return body


class Manager:
    """Plans a request, runs the selected agents in waves, writes the answer."""

    def __init__(
        self,
        settings: Settings | None = None,
        model_chain: Any = None,
        search_chain: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.model_chain = model_chain
        self.search_chain = search_chain
        self.log = logs.get_logger("orchestrator")

    # ------------------------------------------------------------------ api
    async def run(
        self,
        message: str,
        *,
        attachments: list[Attachment] | None = None,
        history: list[dict[str, Any]] | None = None,
        agents: list[str] | None = None,
        planner: str | None = None,
        synthesize: bool | None = None,
        review: bool | None = None,
    ) -> OrchestrationResult:
        started = time.perf_counter()
        attachments = attachments or []
        roster = [name for name in (agents or self.settings.orchestrator_roster) if name in AGENTS]
        if not roster:
            roster = list(AGENTS)
        wants_synthesis = (
            self.settings.orchestrator_synthesize if synthesize is None else synthesize
        )
        wants_review = self.settings.orchestrator_review if review is None else review

        trace = RunTrace()
        trace.add(
            "run",
            "manager",
            "Request received",
            message=message,
            history_turns=len(history or []),
            files=[item.summary() for item in attachments],
            roster=roster,
            planner=planner or self.settings.orchestrator_planner,
            review=bool(wants_review),
            synthesize=bool(wants_synthesis),
            has_model=bool(self.model_chain),
        )

        ctx = AgentContext(
            message=message,
            attachments=attachments,
            model_chain=self.model_chain,
            search_chain=self.search_chain,
            settings=self.settings,
            history=history or [],
            trace=trace,
        )
        plan = await make_plan(
            message,
            attachments,
            model_chain=self.model_chain,
            settings=self.settings,
            roster=roster,
            strategy=planner,
            trace=trace,
        )
        self.log.info(
            "plan %s via %s/%s: %s | deps %s",
            plan.intent,
            plan.strategy,
            plan.detail_source,
            ", ".join(plan.agents),
            {step.agent: step.depends_on for step in plan.steps if step.depends_on} or "none",
        )
        trace.add(
            "planner",
            "planner",
            "Delegating",
            agents=plan.agents,
            waves=[[step.agent for step in wave] for wave in schedule(plan)],
            summary=plan.summary,
        )

        results: list[AgentOutcome] = []
        parallel_started = time.perf_counter()
        if plan.steps:
            results = await run_plan(plan, ctx, settings=self.settings)
        parallel_ms = int((time.perf_counter() - parallel_started) * 1000)
        waves = self._wave_report(plan, results)

        review_info: dict[str, Any] = {
            "requested": False,
            "asked": False,
            "note": "",
            "follow_up": [],
            "reason": "",
        }
        planner_calls = 0 if plan.strategy == "heuristic" else (
            1 if plan.detail_source == "llm" or plan.strategy == "llm" else 0
        )
        model_calls = planner_calls + sum(outcome.model_calls for outcome in results)

        if wants_review and len(results) > 1 and self.model_chain:
            review_info, follow_up_results, review_calls = await self._review(
                message, plan, results, ctx, attachments, history or []
            )
            model_calls += review_calls
            if follow_up_results:
                results = results + follow_up_results
                plan.notes.append(
                    "Manager asked for a follow-up: "
                    + ", ".join(step.agent for step in review_info.get("steps", []))
                )
                waves = self._wave_report(plan, results)
                parallel_ms = int((time.perf_counter() - parallel_started) * 1000)
        else:
            if not wants_review:
                review_info["reason"] = "disabled (ORCHESTRATOR_REVIEW=false)"
            elif len(results) <= 1:
                review_info["reason"] = "one agent answered — nothing to reconcile"
            elif not self.model_chain:
                review_info["reason"] = "no model configured"
            trace.add(
                "merge",
                "manager",
                "Manager review skipped",
                reason=review_info["reason"],
            )

        answer = ""
        provider = model = ""
        synthesized = False
        errors = [
            {"agent": outcome.agent, "error": outcome.error}
            for outcome in results
            if outcome.status == "failed"
        ]

        if wants_synthesis and self.model_chain and any(o.status == "ok" for o in results):
            answer, provider, model = await self._synthesize(
                message, plan, results, attachments, history or [], trace
            )
            synthesized = bool(answer)
            model_calls += 1 if synthesized else 0
        if not answer:
            answer = deterministic_answer(message, results)
            if wants_synthesis and not self.model_chain:
                plan.notes.append(
                    "Synthesis skipped: no model configured — merged the agent outputs."
                )
            trace.add(
                "merge",
                "manager",
                "Answer assembled without a model",
                reason="no model configured" if not self.model_chain else "synthesis returned nothing",
                agents=[outcome.agent for outcome in results],
            )

        result = OrchestrationResult(
            answer=answer,
            plan=plan,
            results=results,
            sources=_collect_sources(results),
            strategy=plan.strategy,
            synthesized=synthesized,
            provider=provider,
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            parallel_ms=parallel_ms,
            errors=errors,
            files=[item.summary() for item in attachments],
            notes=plan.notes,
            run_id=trace.run_id,
            trace=trace,
            waves=waves,
            review=review_info,
            model_calls=model_calls,
            started_at=trace.started_at,
            message=message,
        )

        trace.add(
            "done",
            "manager",
            "Run finished",
            answer=answer,
            latency_ms=result.latency_ms,
            parallel_ms=result.parallel_ms,
            model_calls=model_calls,
            synthesized=synthesized,
            errors=errors,
        )
        if getattr(self.settings, "orchestrator_log_runs", True):
            path = runlog.save_run(result.record())
            result.log_path = str(path) if path else ""
        return result

    # ------------------------------------------------------------- internals
    @staticmethod
    def _wave_report(plan: Plan, results: list[AgentOutcome]) -> list[dict[str, Any]]:
        waves: dict[int, dict[str, Any]] = {}
        for step in plan.steps:
            entry = waves.setdefault(
                step.wave or 1,
                {"wave": step.wave or 1, "agents": [], "depends_on": {}, "latency_ms": 0},
            )
            entry["agents"].append(step.agent)
            if step.depends_on:
                entry["depends_on"][step.agent] = step.depends_on
        for outcome in results:
            entry = waves.setdefault(
                outcome.wave or 1,
                {"wave": outcome.wave or 1, "agents": [], "depends_on": {}, "latency_ms": 0},
            )
            if outcome.agent not in entry["agents"]:
                entry["agents"].append(outcome.agent)
            entry["latency_ms"] = max(entry["latency_ms"], outcome.latency_ms)
        return [waves[key] for key in sorted(waves)]

    async def _review(
        self,
        message: str,
        plan: Plan,
        results: list[AgentOutcome],
        ctx: AgentContext,
        attachments: list[Attachment],
        history: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[AgentOutcome], int]:
        """Let the manager ask for one extra piece of work before answering."""
        info: dict[str, Any] = {
            "requested": True,
            "asked": False,
            "note": "",
            "follow_up": [],
            "reason": "",
        }
        limit = max(int(self.settings.orchestrator_context_chars), 1500)
        messages = [
            {"role": "system", "content": REVIEW_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User request:\n{message}\n\n"
                    f"Plan: {plan.summary}\n\n"
                    f"Attached files:\n{_files_block(attachments)}\n\n"
                    f"What the agents reported:\n\n{_results_block(results, limit)}\n\n"
                    f"Available agents: {', '.join(self.settings.orchestrator_roster)}"
                ),
            },
        ]
        ctx.trace.add_prompt(
            "manager",
            "manager",
            "Manager review prompt (is another step needed?)",
            messages,
        )
        try:
            response = await self.model_chain.complete(
                messages, None, temperature=0.0, max_tokens=min(500, self.settings.llm_max_tokens)
            )
        except Exception as exc:  # noqa: BLE001 - a failed review just means "carry on"
            info["reason"] = f"review call failed ({type(exc).__name__})"
            ctx.trace.add("manager", "manager", "Manager review failed — carrying on", error=str(exc))
            return info, [], 1

        info["asked"] = True
        ctx.trace.add_response("manager", "manager", "Manager review reply", response)
        follow_up = _parse_review(getattr(response, "content", ""), self.settings.orchestrator_roster)
        info["note"] = follow_up.get("note", "")
        steps: list[PlanStep] = []
        for item in follow_up.get("follow_up", [])[:2]:
            step = PlanStep(
                agent=item["agent"],
                task=item["task"],
                reason=item.get("reason") or "requested by the manager after reading the results",
                depends_on=[],
                wave=(max((outcome.wave for outcome in results), default=1) + 1),
            )
            step.inputs = {"from_agents": [outcome.agent for outcome in results if outcome.status == "ok"]}
            step.depends_on = [outcome.agent for outcome in results if outcome.status == "ok"][:3]
            steps.append(step)
            plan.steps.append(step)
            info["follow_up"].append(
                {"agent": step.agent, "task": step.task, "reason": step.reason}
            )
        if not steps:
            info["reason"] = "the manager saw no gap"
            ctx.trace.add(
                "merge",
                "manager",
                "Manager review: no extra work needed",
                note=info["note"],
            )
            return info, [], 1

        info["steps"] = steps
        ctx.trace.add(
            "manager",
            "manager",
            "Manager asked for more work",
            requests=[
                {"agent": step.agent, "task": step.task, "reason": step.reason}
                for step in steps
            ],
            note=info["note"],
        )
        extra_plan = Plan(intent=plan.intent, summary=plan.summary, steps=steps)
        offset = max((outcome.wave for outcome in results), default=1)
        extras = await run_plan(extra_plan, ctx, settings=self.settings, wave_offset=offset)
        return info, extras, 1

    async def _synthesize(
        self,
        message: str,
        plan: Plan,
        results: list[AgentOutcome],
        attachments: list[Attachment],
        history: list[dict[str, Any]],
        trace: RunTrace,
    ) -> tuple[str, str, str]:
        limit = max(int(self.settings.orchestrator_context_chars), 1500)
        messages: list[dict[str, Any]] = [{"role": "system", "content": MANAGER_PROMPT}]
        for item in history[-6:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                messages.append(
                    {"role": item["role"], "content": str(item.get("content", ""))[:3000]}
                )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"User request:\n{message}\n\n"
                    f"Attached files:\n{_files_block(attachments)}\n\n"
                    f"Plan: {plan.summary}\n"
                    f"Intent: {plan.intent}\n\n"
                    f"Specialist reports:\n\n{_results_block(results, limit)}"
                ),
            }
        )
        trace.add_prompt("synthesis", "manager", "Final synthesis prompt", messages)
        try:
            response = await self.model_chain.complete(
                messages,
                None,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the deterministic merge
            self.log.warning("synthesis failed, merging deterministically: %s", exc)
            plan.notes.append(f"Synthesis call failed ({type(exc).__name__}); merged agent outputs.")
            trace.add("synthesis", "manager", "Synthesis failed — merged agent outputs", error=str(exc))
            return "", "", ""
        trace.add_response("synthesis", "manager", "Final answer", response)
        return response.content, response.provider, response.model


def _parse_review(raw: str, roster: list[str]) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return {"note": "", "follow_up": []}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"note": "", "follow_up": []}
    items: list[dict[str, str]] = []
    for item in payload.get("follow_up") or []:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent") or "").strip().lower()
        task = str(item.get("task") or "").strip()
        if agent in roster and task:
            items.append(
                {
                    "agent": agent,
                    "task": task[:1200],
                    "reason": str(item.get("reason") or "")[:300],
                }
            )
    return {"note": str(payload.get("note") or "")[:300], "follow_up": items}


def agent_catalogue(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "agents": catalogue(),
        "roster": settings.orchestrator_roster,
        "planner": settings.orchestrator_planner,
        "review": settings.orchestrator_review,
        "synthesize": settings.orchestrator_synthesize,
        "timeout_s": settings.orchestrator_timeout,
        "parallel": settings.orchestrator_max_parallel,
        "trace": {
            "chars": settings.orchestrator_trace_chars,
            "log_runs": settings.orchestrator_log_runs,
        },
        "enabled": settings.orchestrator_enabled,
    }
