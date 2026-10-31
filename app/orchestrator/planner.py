"""Planner: read the request, decide which agents run and what each one does.

Two planners, same output:

* **heuristic** (default) — rules over the prompt and the attachments. Free,
  deterministic, no extra model call, ~0 ms.
* **llm** (``ORCHESTRATOR_PLANNER=llm``) — one small completion that returns a
  JSON plan. More adaptive on odd requests, but it costs a call and 1–3 s, and
  falls back to the heuristic plan if the model returns anything unusable.

The plan is data, not prose: a list of steps, each with an agent, a task
instruction and the reason it was chosen. The UI shows exactly that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.orchestrator.files import Attachment

# Agents, in display order.
AGENTS: dict[str, dict[str, str]] = {
    "document": {
        "label": "Document analyst",
        "description": "Reads the attached files and answers from their content.",
    },
    "search": {
        "label": "Web researcher",
        "description": "Searches the web, reads pages and returns answers with sources.",
    },
    "data": {
        "label": "Data analyst",
        "description": "Computes statistics over tables, JSON or numbers in the request.",
    },
    "general": {
        "label": "Generalist",
        "description": "Answers from the model's own knowledge, no tools.",
    },
}

# ------------------------------------------------------------------ signals

_SEARCH_SIGNALS = (
    "latest", "today", "tonight", "yesterday", "this week", "this month", "recent",
    "news", "current", "currently", "right now", "up to date", "up-to-date",
    "price", "prices", "stock", "release", "released", "changelog", "version",
    "who is", "who won", "when did", "when will", "schedule", "weather",
    "search", "google", "look up", "find out", "browse", "web", "online",
    "source", "sources", "cite", "citation", "link", "links", "statistics",
)
# Wording that asks for reasoning, not just retrieval. A generalist is only worth
# a completion when the request wants an explanation on top of the other agents.
_REASON_SIGNALS = (
    "explain", "why", "how does", "how do", "how would", "compare", "comparison",
    "pros", "cons", "tradeoff", "trade-off", "difference", "differences", "design",
    "architect", "architecture", "critique", "recommend", "should i", "better",
    "best way", "write a", "draft", "discuss", "implications", "approach",
)

_DATA_SIGNALS = (
    "average", "mean", "median", "sum", "total", "count", "how many", "percentage",
    "percent", "ratio", "correlation", "trend", "growth", "distribution", "variance",
    "std", "standard deviation", "max", "maximum", "min", "minimum", "compare",
    "top", "bottom", "rank", "chart", "plot", "table", "rows", "columns", "dataset",
    "csv", "spreadsheet", "calculate", "compute", "analyse", "analyze", "analysis",
    "breakdown", "forecast", "regression", "outlier",
)
_DOCUMENT_SIGNALS = (
    "this file", "the file", "attached", "attachment", "document", "pdf", "docx",
    "in the text", "according to", "summarize", "summarise", "summary", "extract",
    "quote", "clause", "contract", "invoice", "resume", "cv", "report", "memo",
    "transcript", "policy", "spec", "read this", "review this",
)
_NUMBER = re.compile(r"(?<![\w.])(-?\d+(?:[.,]\d+)?)(?![\w.])")


def _hits(text: str, signals: tuple[str, ...]) -> list[str]:
    """Whole-word signal matching: "sum" must not fire on "summarise"."""
    lowered = text.lower()
    found: list[str] = []
    for signal in signals:
        pattern = r"(?<![a-z0-9])" + re.escape(signal) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            found.append(signal)
    return found


def _numbers_in(text: str) -> list[float]:
    values: list[float] = []
    for raw in _NUMBER.findall(text):
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return values


# --------------------------------------------------------------------- plan


@dataclass
class PlanStep:
    agent: str
    task: str
    reason: str
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return AGENTS.get(self.agent, {}).get("label", self.agent.title())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "label": self.label,
            "task": self.task,
            "reason": self.reason,
            "inputs": self.inputs,
        }


@dataclass
class Plan:
    intent: str
    summary: str
    steps: list[PlanStep]
    strategy: str = "heuristic"
    signals: dict[str, list[str]] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def agents(self) -> list[str]:
        return [step.agent for step in self.steps]

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "summary": self.summary,
            "strategy": self.strategy,
            "signals": self.signals,
            "steps": [step.to_dict() for step in self.steps],
            "agents": self.agents,
            "files": self.files,
            "notes": self.notes,
            "parallel": len(self.steps) > 1,
        }


def _tabular(attachments: list[Attachment]) -> list[Attachment]:
    return [item for item in attachments if item.rows and len(item.rows) > 1]


def _file_list(attachments: list[Attachment]) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "kind": item.kind,
            "chars": item.chars,
            "rows": (len(item.rows) - 1) if item.rows else 0,
            "truncated": item.truncated,
        }
        for item in attachments
    ]


def heuristic_plan(
    message: str,
    attachments: list[Attachment] | None = None,
    *,
    roster: list[str] | None = None,
) -> Plan:
    """Rules → plan. Never fails, never calls a model."""
    attachments = attachments or []
    roster = roster or list(AGENTS)
    text = message or ""
    search_hits = _hits(text, _SEARCH_SIGNALS)
    data_hits = _hits(text, _DATA_SIGNALS)
    doc_hits = _hits(text, _DOCUMENT_SIGNALS) if attachments else []
    numbers = _numbers_in(text)
    tables = _tabular(attachments)

    steps: list[PlanStep] = []
    notes: list[str] = []

    # ---- document analyst ------------------------------------------------
    if "document" in roster and attachments:
        names = ", ".join(item.name for item in attachments[:4])
        task = (
            f"Read the attached file(s) ({names}) and answer this request strictly from "
            f"their content: {text[:600]!r}. Quote the relevant lines and say which file "
            "and, when visible, which section or page they come from. If the files do not "
            "contain the answer, say exactly what is missing."
        )
        steps.append(
            PlanStep(
                agent="document",
                task=task,
                reason=(
                    f"{len(attachments)} attachment(s) provided"
                    + (f" and the request refers to a document ({doc_hits[0]!r})" if doc_hits else "")
                ),
                inputs={"files": [item.id for item in attachments]},
            )
        )

    # ---- data analyst ----------------------------------------------------
    data_reason = ""
    if tables:
        data_reason = f"tabular attachment ({', '.join(item.name for item in tables[:2])})"
    elif data_hits:
        data_reason = f"analysis wording ({', '.join(data_hits[:3])})"
    elif len(numbers) >= 3:
        data_reason = f"{len(numbers)} numbers in the request"
    if "data" in roster and data_reason:
        numeric_note = (
            " Use the numeric columns you find." if tables else " Use the numbers in the request."
        )
        steps.append(
            PlanStep(
                agent="data",
                task=(
                    f"Analyse the quantitative side of this request: {text[:600]!r}."
                    + numeric_note
                    + " Report the figures you actually computed (count, sum, mean, median, "
                    "min, max, spread) and what they imply. Do not invent numbers."
                ),
                reason=data_reason,
                inputs={"files": [item.id for item in tables], "numbers": numbers[:20]},
            )
        )

    # ---- web researcher --------------------------------------------------
    if "search" in roster and search_hits:
        steps.append(
            PlanStep(
                agent="search",
                task=(
                    f"Find current, verifiable information for: {text[:600]!r}. "
                    "Prefer primary sources, note dates, and return the URLs you used."
                ),
                reason=f"time-sensitive or source-needing wording ({', '.join(search_hits[:3])})",
                inputs={"query": text[:300]},
            )
        )

    # ---- generalist ------------------------------------------------------
    # No specialists: the generalist is the whole plan.
    # With specialists: only add it when the request also asks for reasoning.
    # The manager already writes the final answer, so a generalist on every
    # lookup would just burn one extra completion for nothing.
    reason_hits = _hits(text, _REASON_SIGNALS)
    if "general" in roster and (not steps or reason_hits):
        reason = (
            "no tool is needed — this is a knowledge question"
            if not steps
            else f"the request asks for reasoning ({', '.join(reason_hits[:3])}) on top of the findings"
        )
        steps.append(
            PlanStep(
                agent="general",
                task=(
                    f"Answer this request from your own knowledge, clearly and briefly: "
                    f"{text[:600]!r}. State your uncertainty where it matters."
                ),
                reason=reason,
            )
        )

    if not steps:
        steps.append(PlanStep(agent="general", task=text[:600], reason="fallback"))

    if not any(step.agent == "general" for step in steps):
        notes.append("Generalist skipped: the specialists cover this request.")

    intent = _intent(steps, attachments, data_hits, search_hits)
    summary = _summary(steps, attachments)
    return Plan(
        intent=intent,
        summary=summary,
        steps=steps,
        strategy="heuristic",
        signals={
            "search": search_hits[:6],
            "data": data_hits[:6],
            "document": doc_hits[:6],
            "numbers": [str(value) for value in numbers[:6]],
        },
        files=_file_list(attachments),
        notes=notes,
    )


def _intent(
    steps: list[PlanStep],
    attachments: list[Attachment],
    data_hits: list[str],
    search_hits: list[str],
) -> str:
    agents = {step.agent for step in steps}
    if {"document", "data"} <= agents:
        return "document-analysis"
    if {"document", "search"} <= agents:
        return "document-cross-check"
    if "document" in agents:
        return "document-qa"
    if {"search", "data"} <= agents:
        return "research-analysis"
    if "search" in agents:
        return "research"
    if "data" in agents:
        return "analysis"
    return "general"


def _summary(steps: list[PlanStep], attachments: list[Attachment]) -> str:
    labels = ", ".join(step.label for step in steps)
    files = f"{len(attachments)} file(s)" if attachments else "no files"
    if len(steps) == 1:
        return f"{labels} handles this alone ({files}), then the manager writes the answer."
    return f"{labels} work in parallel ({files}), then the manager merges their answers."


# --------------------------------------------------------------- llm planner

PLANNER_PROMPT = (
    "You are the planner of a small agent team. Decide which specialists should work on "
    "the user's request.\n"
    "Available agents:\n"
    "- document: reads attached files (text/PDF/DOCX/CSV)\n"
    "- search: looks things up on the web, returns sources\n"
    "- data: computes statistics over tables or numbers\n"
    "- general: answers from its own knowledge\n"
    "Reply with ONLY a JSON object:\n"
    '{"intent": "...", "summary": "...", "steps": [{"agent": "...", '
    '"task": "instruction for that agent", "reason": "why"}]}\n'
    "Rules: 1-4 steps, never duplicate an agent, use document only when files are "
    "attached, use search only for time-sensitive or source-needing requests, and always "
    "include general unless two specialists already cover the request."
)


def _parse_llm_plan(raw: str, roster: list[str]) -> list[PlanStep] | None:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    steps: list[PlanStep] = []
    seen: set[str] = set()
    for item in raw_steps[:4]:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent") or "").strip().lower()
        if agent not in AGENTS or agent in seen or agent not in roster:
            continue
        seen.add(agent)
        steps.append(
            PlanStep(
                agent=agent,
                task=str(item.get("task") or "")[:1200],
                reason=str(item.get("reason") or "chosen by the planner model")[:300],
            )
        )
    return steps or None


async def llm_plan(
    message: str,
    attachments: list[Attachment],
    model_chain: Any,
    settings: Any,
    roster: list[str] | None = None,
) -> Plan | None:
    """Ask the model for a plan. ``None`` means "use the heuristic instead"."""
    roster = roster or list(AGENTS)
    files_block = "\n".join(
        f"- {item.name} ({item.kind}, {item.chars} chars"
        + (f", {len(item.rows) - 1} rows" if item.rows else "")
        + f"): {item.preview(200)}"
        for item in attachments
    ) or "(none)"
    messages = [
        {"role": "system", "content": PLANNER_PROMPT},
        {
            "role": "user",
            "content": f"Attached files:\n{files_block}\n\nRequest:\n{message[:2000]}",
        },
    ]
    try:
        response = await model_chain.complete(
            messages,
            None,
            temperature=0.0,
            max_tokens=min(600, settings.llm_max_tokens),
        )
    except Exception:
        return None
    steps = _parse_llm_plan(getattr(response, "content", ""), roster)
    if not steps:
        return None
    return Plan(
        intent="model-planned",
        summary=_summary(steps, attachments),
        steps=steps,
        strategy="llm",
        files=_file_list(attachments),
    )


async def make_plan(
    message: str,
    attachments: list[Attachment] | None = None,
    *,
    model_chain: Any = None,
    settings: Any = None,
    roster: list[str] | None = None,
    strategy: str | None = None,
) -> Plan:
    """Pick the planner from settings, falling back to the heuristic plan."""
    attachments = attachments or []
    roster = roster or list(AGENTS)
    strategy = (strategy or getattr(settings, "orchestrator_planner", "heuristic") or "").lower()
    base = heuristic_plan(message, attachments, roster=roster)
    if strategy == "llm" and model_chain is not None and settings is not None:
        planned = await llm_plan(message, attachments, model_chain, settings, roster)
        if planned is not None:
            merged = heuristic_plan(message, attachments, roster=roster)
            covered = set(planned.agents)
            planned.notes = [
                note for note in merged.notes if note not in {"Generalist skipped: the specialists cover this request."}
            ]
            for step in merged.steps:
                if step.agent in covered:
                    continue
                planned.notes.append(
                    f"Heuristic would have added {step.label} ({step.reason}); the planner model did not."
                )
            return planned
        base.notes.append("Planner model returned no usable plan; used the heuristic plan.")
    return base
