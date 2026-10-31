"""Manager: plan → delegate in parallel → wrap the results into one answer.

The manager is deliberately thin. Planning and synthesis are the only places a
model *may* be used:

* ``ORCHESTRATOR_PLANNER=heuristic`` (default) → rules plan, free and instant.
* ``ORCHESTRATOR_PLANNER=llm``              → the model writes the plan (+1 call).
* ``ORCHESTRATOR_SYNTHESIZE=true`` (default) → the model writes the closing answer.
  Set it to ``false`` for a deterministic merge: cheaper and faster, less fluent.

So a plain request costs one completion (synthesis) plus whatever the selected
agents need — not one call per agent hop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app import logs
from app.config import Settings, get_settings
from app.orchestrator.agents import AgentContext, AgentOutcome, catalogue
from app.orchestrator.executor import run_plan
from app.orchestrator.files import Attachment
from app.orchestrator.planner import AGENTS, Plan, make_plan

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


@dataclass
class OrchestrationResult:
    answer: str
    plan: Plan
    results: list[AgentOutcome]
    sources: list[str] = field(default_factory=list)
    strategy: str = "heuristic"
    synthesized: bool = False
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    parallel_ms: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "mode": "auto",
            "orchestration": {
                "strategy": self.strategy,
                "synthesized": self.synthesized,
                "provider": self.provider,
                "model": self.model,
                "latency_ms": self.latency_ms,
                "parallel_ms": self.parallel_ms,
                "files": self.files,
                "notes": self.notes,
                "plan": self.plan.to_dict(),
                "agents": [outcome.to_dict() for outcome in self.results],
            },
            "sources": self.sources,
            "errors": self.errors,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }


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
    """Plans a request, runs the selected agents in parallel, writes the answer."""

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
    ) -> OrchestrationResult:
        started = time.perf_counter()
        attachments = attachments or []
        roster = [name for name in (agents or self.settings.orchestrator_roster) if name in AGENTS]
        if not roster:
            roster = list(AGENTS)
        wants_synthesis = (
            self.settings.orchestrator_synthesize if synthesize is None else synthesize
        )

        ctx = AgentContext(
            message=message,
            attachments=attachments,
            model_chain=self.model_chain,
            search_chain=self.search_chain,
            settings=self.settings,
            history=history or [],
        )
        plan = await make_plan(
            message,
            attachments,
            model_chain=self.model_chain,
            settings=self.settings,
            roster=roster,
            strategy=planner,
        )
        self.log.info(
            "plan %s via %s: %s", plan.intent, plan.strategy, ", ".join(plan.agents)
        )

        results: list[AgentOutcome] = []
        parallel_started = time.perf_counter()
        if plan.steps:
            results = await run_plan(plan, ctx, settings=self.settings)
        parallel_ms = int((time.perf_counter() - parallel_started) * 1000)

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
                message, plan, results, attachments, history or []
            )
            synthesized = bool(answer)
        if not answer:
            answer = deterministic_answer(message, results)
            if wants_synthesis and not self.model_chain:
                plan.notes.append("Synthesis skipped: no model configured — merged the agent outputs.")

        return OrchestrationResult(
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
        )

    # ------------------------------------------------------------ synthesis
    async def _synthesize(
        self,
        message: str,
        plan: Plan,
        results: list[AgentOutcome],
        attachments: list[Attachment],
        history: list[dict[str, Any]],
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
            return "", "", ""
        return response.content, response.provider, response.model


def agent_catalogue(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "agents": catalogue(),
        "roster": settings.orchestrator_roster,
        "planner": settings.orchestrator_planner,
        "synthesize": settings.orchestrator_synthesize,
        "timeout_s": settings.orchestrator_timeout,
        "parallel": settings.orchestrator_max_parallel,
        "enabled": settings.orchestrator_enabled,
    }
