"""The specialists the manager can call.

Every agent has the same shape — ``async run(step, context) -> AgentOutcome`` —
so the executor can fan them out with ``asyncio.gather`` and the manager can
merge whatever comes back, including failures.

* ``document``  reads attached files (retrieval → grounded answer, quotes)
* ``search``    runs the existing tool loop (web_search / fetch_url) with sources
* ``data``      computes statistics deterministically, then explains them
* ``general``   plain completion, no tools

Each agent writes its own steps into the run trace: the task it was given, the
exact prompt it sent to the model, every tool call and result, and its answer.
That is what the UI shows under "How this answer was built".
"""

from __future__ import annotations

import ast
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.orchestrator import host
from app.orchestrator.contracts import AgentLoopFactory, OrchestratorSettings
from app.orchestrator.files import Attachment
from app.orchestrator.planner import AGENTS, PlanStep


@dataclass
class AgentOutcome:
    agent: str
    status: str  # ok | failed | skipped
    answer: str = ""
    task: str = ""
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    error: str = ""
    hint: str = ""
    latency_ms: int = 0
    provider: str = ""
    model: str = ""
    detail: str = ""
    wave: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    depends_on: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return AGENTS.get(self.agent, {}).get("label", self.agent.title())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "label": self.label,
            "status": self.status,
            "answer": self.answer,
            "task": self.task,
            "reason": self.reason,
            "data": self.data,
            "sources": self.sources,
            "error": self.error,
            "hint": self.hint,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "model": self.model,
            "detail": self.detail,
            "wave": self.wave,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "depends_on": self.depends_on,
        }


@dataclass
class AgentContext:
    """Everything an agent may need, assembled once per request."""

    message: str
    attachments: list[Attachment]
    model_chain: Any
    search_chain: Any
    settings: OrchestratorSettings
    history: list[dict[str, Any]] = field(default_factory=list)
    trace: Any = None
    upstream: dict[str, "AgentOutcome"] = field(default_factory=dict)
    # injected by the service/manager: the single-agent tool loop for `search`
    agent_loop: AgentLoopFactory | None = None

    @property
    def has_model(self) -> bool:
        return bool(self.model_chain)

    # ------------------------------------------------------------- tracing
    def emit(self, stage: str, actor: str, title: str, **detail: Any) -> None:
        if self.trace is not None:
            self.trace.add(stage, actor, title, **detail)

    def emit_prompt(
        self,
        stage: str,
        actor: str,
        title: str,
        messages: list[dict[str, Any]],
        **detail: Any,
    ) -> None:
        if self.trace is not None:
            self.trace.add_prompt(stage, actor, title, messages, **detail)

    def emit_response(
        self, stage: str, actor: str, title: str, response: Any, **detail: Any
    ) -> None:
        if self.trace is not None:
            self.trace.add_response(stage, actor, title, response, **detail)

    def upstream_block(self, step: PlanStep) -> tuple[str, list[str]]:
        """Text + agent names from agents this step depends on."""
        parts: list[str] = []
        used: list[str] = []
        for name in step.depends_on:
            outcome = self.upstream.get(name)
            if outcome is None:
                continue
            used.append(name)
            body = outcome.answer.strip() or (outcome.error or "(no output)")
            parts.append(f"### {outcome.label} ({name}) — {outcome.status}\n{body[:3000]}")
        if not parts:
            return "", []
        block = (
            "\n\nFindings from agents that ran before you. Use them where they help, "
            "do not repeat them, and do not contradict them without saying why:\n\n"
            + "\n\n".join(parts)
        )
        return block, used


# ---------------------------------------------------------------- retrieval

_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which", "when",
    "where", "how", "why", "are", "was", "were", "does", "did", "can", "you",
    "please", "tell", "about", "into", "have", "has", "had", "not", "but", "all",
    "any", "use", "using", "give", "list", "make", "made", "find", "show", "them",
}


def _terms(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP}


def chunks(attachment: Attachment, size: int = 1400, overlap: int = 150) -> list[str]:
    text = attachment.text
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        out.append(text[start : start + size])
        start += size - overlap
        if len(out) > 400:  # hard stop: never explode on a huge file
            break
    return out


def rank_chunks(
    attachments: list[Attachment], query: str, limit: int
) -> list[tuple[Attachment, str, float]]:
    """Cheap keyword retrieval — no embedding model, no extra dependency."""
    wanted = _terms(query)
    scored: list[tuple[Attachment, str, float]] = []
    for attachment in attachments:
        for index, chunk in enumerate(chunks(attachment)):
            words = _terms(chunk)
            if not words:
                continue
            overlap = len(wanted & words) / max(len(wanted), 1)
            # keep the start of the document competitive: it usually holds the summary
            boost = 0.15 if index == 0 else 0.0
            scored.append((attachment, chunk, overlap + boost))
    scored.sort(key=lambda item: item[2], reverse=True)
    return scored[:limit]


def _context_block(scored: list[tuple[Attachment, str, float]], budget: int) -> str:
    parts: list[str] = []
    used = 0
    for attachment, chunk, _score in scored:
        header = f"[{attachment.name}]"
        piece = f"{header}\n{chunk.strip()}"
        if used + len(piece) > budget:
            piece = piece[: max(budget - used, 0)]
        if not piece.strip():
            break
        parts.append(piece)
        used += len(piece)
        if used >= budget:
            break
    return "\n\n---\n\n".join(parts)


def _merge_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in history or []:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
            merged.append({"role": item["role"], "content": str(item.get("content", ""))[:4000]})
    return merged[-8:]


# ------------------------------------------------------------------ general

GENERAL_PROMPT = (
    "You are the generalist on a small agent team. Answer the request directly and "
    "concisely from your own knowledge. Do not claim to have searched the web or read "
    "files you were not given. If you are unsure, say which part is uncertain."
)


async def run_general(step: PlanStep, ctx: AgentContext) -> AgentOutcome:
    started = time.perf_counter()
    upstream_text, used = ctx.upstream_block(step)
    if not ctx.has_model:
        return AgentOutcome(
            agent="general",
            status="failed",
            task=step.task,
            reason=step.reason,
            error="No model provider is configured.",
            hint="Set LLM_PROVIDER and a provider key + model in .env.",
            latency_ms=int((time.perf_counter() - started) * 1000),
            depends_on=list(step.depends_on),
        )
    messages = [{"role": "system", "content": GENERAL_PROMPT}]
    messages.extend(_merge_history(ctx.history))
    messages.append({"role": "user", "content": (step.task or ctx.message) + upstream_text})
    ctx.emit(
        "agent",
        "general",
        "Task received",
        task=step.task,
        reason=step.reason,
        from_agents=used,
        history_turns=len(_merge_history(ctx.history)),
    )
    ctx.emit_prompt("agent", "general", "Prompt sent to the model", messages, kind="general")
    try:
        response = await ctx.model_chain.complete(
            messages,
            None,
            temperature=ctx.settings.llm_temperature,
            max_tokens=ctx.settings.llm_max_tokens,
        )
    except Exception as exc:
        ctx.emit("agent", "general", "Model call failed", error=f"{type(exc).__name__}: {exc}")
        raise
    ctx.emit_response("agent", "general", "Model reply", response)
    return AgentOutcome(
        agent="general",
        status="ok",
        answer=response.content,
        task=step.task,
        reason=step.reason,
        provider=response.provider,
        model=response.model,
        latency_ms=int((time.perf_counter() - started) * 1000),
        model_calls=1,
        depends_on=list(step.depends_on),
    )


# ------------------------------------------------------------------- search

async def run_search(step: PlanStep, ctx: AgentContext) -> AgentOutcome:
    started = time.perf_counter()
    upstream_text, used = ctx.upstream_block(step)
    calls = {"model": 0, "tool": 0}

    if not ctx.has_model:
        # No model: still useful to run the chain and hand back raw results.
        ctx.emit(
            "agent",
            "search",
            "Task received",
            task=step.task,
            reason=step.reason,
            from_agents=used,
            note="no model configured — returning raw search results",
        )
        try:
            response = await ctx.search_chain.search(ctx.message[:300], ctx.settings.agent_max_search_results)
        except Exception as exc:
            ctx.emit("tool", "search", "web_search failed", kind="result", ok=False, error=str(exc))
            return AgentOutcome(
                agent="search",
                status="failed",
                task=step.task,
                reason=step.reason,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                depends_on=list(step.depends_on),
            )
        ctx.emit(
            "tool",
            "search",
            f"web_search '{response.query}'",
            kind="result",
            tool="web_search",
            ok=True,
            provider=response.provider,
            results=[
                {"title": item.title, "url": item.url, "snippet": item.snippet}
                for item in response.results
            ],
        )
        lines = [f"- {item.title} — {item.url}" for item in response.results]
        return AgentOutcome(
            agent="search",
            status="ok",
            answer="Search results (no model configured, so no summary):\n" + "\n".join(lines),
            task=step.task,
            reason=step.reason,
            sources=[item.url for item in response.results],
            detail=f"{len(response.results)} raw result(s) from {response.provider}",
            latency_ms=int((time.perf_counter() - started) * 1000),
            tool_calls=1,
            depends_on=list(step.depends_on),
        )

    def observe(kind: str, payload: dict[str, Any]) -> None:
        if kind == "prompt":
            calls["model"] += 1
            ctx.emit_prompt(
                "agent",
                "search",
                f"Prompt sent to the model (step {payload.get('step')})",
                payload["messages"],
                kind="search",
                tools=["web_search", "fetch_url"],
            )
        elif kind == "model":
            ctx.emit_response(
                "agent",
                "search",
                f"Model reply (step {payload.get('step')})",
                payload["response"],
            )
        elif kind == "tool":
            call = payload["call"]
            calls["tool"] += 1
            ctx.emit(
                "tool",
                "search",
                f"Tool call: {call.name}",
                kind="call",
                tool=call.name,
                arguments=call.arguments,
                step=payload.get("step"),
            )
            ctx.emit(
                "tool",
                "search",
                f"Tool result: {call.name}",
                kind="result",
                tool=call.name,
                ok=bool(payload["output"].get("ok")),
                latency_ms=payload.get("latency_ms"),
                output=payload["output"],
            )

    ctx.emit(
        "agent",
        "search",
        "Task received",
        task=step.task,
        reason=step.reason,
        from_agents=used,
        max_steps=ctx.settings.agent_max_steps,
    )
    factory = ctx.agent_loop or host.agent_loop()
    agent = factory(ctx.model_chain, ctx.search_chain, ctx.settings, observer=observe)
    extra = ""
    if ctx.attachments:
        names = ", ".join(item.name for item in ctx.attachments)
        extra = (
            f"\n\nContext: the user also attached {names}. Use them only if the request "
            "refers to them; do not pretend they were searched online."
        )
    result = await agent.run(step.task + upstream_text + extra, history=ctx.history, allow_search=True)
    return AgentOutcome(
        agent="search",
        status="ok" if result.answer else "failed",
        answer=result.answer,
        task=step.task,
        reason=step.reason,
        sources=result.sources,
        provider=result.provider,
        model=result.model,
        error="" if result.answer else "the search agent produced no answer",
        detail=f"{result.steps} step(s) · search via {result.search_provider or 'n/a'}",
        latency_ms=int((time.perf_counter() - started) * 1000),
        data={"trace": result.trace[:12], "search_provider": result.search_provider},
        model_calls=calls["model"],
        tool_calls=calls["tool"],
        depends_on=list(step.depends_on),
    )


# ----------------------------------------------------------------- document

DOCUMENT_PROMPT = (
    "You are the document analyst on a small agent team. Answer strictly from the "
    "excerpts below — they were retrieved from the user's attachments.\n"
    "Rules: quote the exact lines you rely on, name the file for every claim, and if the "
    "excerpts do not contain the answer say so plainly instead of filling the gap from "
    "memory. Never invent a page number, a number or a clause."
)


async def run_document(step: PlanStep, ctx: AgentContext) -> AgentOutcome:
    started = time.perf_counter()
    if not ctx.attachments:
        return AgentOutcome(
            agent="document",
            status="skipped",
            task=step.task,
            reason=step.reason,
            detail="no attachments in this request",
            depends_on=list(step.depends_on),
        )
    budget = max(int(ctx.settings.files_context_chars), 1500)
    scored = rank_chunks(ctx.attachments, f"{ctx.message} {step.task}", limit=8)
    context = _context_block(scored, budget)
    files = ", ".join(item.name for item in ctx.attachments)
    upstream_text, used = ctx.upstream_block(step)

    ctx.emit(
        "agent",
        "document",
        "Task received",
        task=step.task,
        reason=step.reason,
        from_agents=used,
        files=[item.summary() for item in ctx.attachments],
    )
    ctx.emit(
        "agent",
        "document",
        "Retrieved excerpts (keyword ranking, no embeddings)",
        query=f"{ctx.message} {step.task}"[:300],
        budget_chars=budget,
        excerpts=[
            {
                "file": attachment.name,
                "score": round(score, 3),
                "chars": len(chunk),
                "preview": re.sub(r"\s+", " ", chunk.strip())[:240],
            }
            for attachment, chunk, score in scored
        ],
    )

    if not ctx.has_model:
        # Extractive fallback: honest, useful, no model required.
        excerpt = context[:budget]
        return AgentOutcome(
            agent="document",
            status="ok",
            answer=(
                f"Extracted from {files} (no model configured, so this is a raw extract "
                "rather than a written answer):\n\n" + excerpt
            ),
            task=step.task,
            reason=step.reason,
            detail="extractive (no model)",
            data={"files": [item.summary() for item in ctx.attachments]},
            latency_ms=int((time.perf_counter() - started) * 1000),
            depends_on=list(step.depends_on),
        )

    messages = [
        {"role": "system", "content": DOCUMENT_PROMPT},
        {
            "role": "user",
            "content": (
                f"Request: {ctx.message}\n\nYour task: {step.task}\n\n"
                f"Attached files: {files}\n\nExcerpts:\n\n{context}"
            )
            + upstream_text,
        },
    ]
    ctx.emit_prompt(
        "agent",
        "document",
        "Prompt sent to the model",
        messages,
        kind="document",
        excerpts=len(scored),
        context_chars=len(context),
    )
    try:
        response = await ctx.model_chain.complete(
            messages, None, temperature=0.1, max_tokens=ctx.settings.llm_max_tokens
        )
    except Exception as exc:
        ctx.emit("agent", "document", "Model call failed", error=f"{type(exc).__name__}: {exc}")
        raise
    ctx.emit_response("agent", "document", "Model reply", response)
    return AgentOutcome(
        agent="document",
        status="ok" if response.content else "failed",
        answer=response.content,
        task=step.task,
        reason=step.reason,
        provider=response.provider,
        model=response.model,
        detail=f"{len(ctx.attachments)} file(s), {len(scored)} excerpt(s)",
        data={"files": [item.summary() for item in ctx.attachments]},
        latency_ms=int((time.perf_counter() - started) * 1000),
        model_calls=1,
        depends_on=list(step.depends_on),
    )


# --------------------------------------------------------------------- data

_ARITH = re.compile(r"^[\d\s+\-*/().]+$")


def _safe_arithmetic(text: str) -> tuple[str, float] | None:
    """Evaluate ``12 + 30 + 45``-style expressions without ``eval``."""
    candidate = text.strip().rstrip("?=.")
    if not candidate or not _ARITH.match(candidate) or not re.search(r"[+\-*/]", candidate):
        return None
    try:
        tree = ast.parse(candidate, mode="eval")
    except SyntaxError:
        return None

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = walk(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right if right else math.inf
        raise ValueError("unsupported expression")

    try:
        return candidate, walk(tree)
    except (ValueError, ZeroDivisionError):
        return None


def _stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {}
    result: dict[str, float | int] = {
        "count": len(values),
        "sum": round(sum(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "median": round(statistics.median(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }
    if len(values) > 1:
        result["stdev"] = round(statistics.stdev(values), 6)
    return result


def _to_number(value: str) -> float | None:
    cleaned = str(value).strip().replace("$", "").replace("%", "").replace(",", "")
    if cleaned in ("", "-", "n/a", "NA", "null", "None"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def analyse_table(attachment: Attachment) -> dict[str, Any]:
    """Column profiles for a parsed table (CSV/TSV/JSON/XLSX)."""
    rows = attachment.rows or []
    header = attachment.header or []
    body = rows[1:] if header else rows
    profile: dict[str, Any] = {
        "file": attachment.name,
        "rows": len(body),
        "columns": len(header) or (len(body[0]) if body else 0),
        "numeric": {},
        "categorical": {},
        "missing": {},
    }
    for index, name in enumerate(header):
        column = [row[index] if index < len(row) else "" for row in body]
        numbers = [value for value in (_to_number(cell) for cell in column) if value is not None]
        missing = sum(1 for cell in column if str(cell).strip() == "")
        if numbers and len(numbers) >= max(1, len(column) // 2):
            stats = _stats(numbers)
            profile["numeric"][name] = {**stats, "missing": missing}
        else:
            counts: dict[str, int] = {}
            for cell in column:
                key = str(cell).strip() or "(blank)"
                counts[key] = counts.get(key, 0) + 1
            top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:5]
            profile["categorical"][name] = {"unique": len(counts), "top": top}
            profile["missing"][name] = missing
    return profile


def _data_answer(profiles: list[dict[str, Any]], expression: tuple[str, float] | None, numbers: list[float]) -> str:
    lines: list[str] = []
    for profile in profiles:
        lines.append(f"{profile['file']}: {profile['rows']} row(s), {profile['columns']} column(s).")
        for column, stats in list(profile["numeric"].items())[:6]:
            lines.append(
                f"  · {column}: sum {stats['sum']}, mean {stats['mean']}, median {stats['median']}, "
                f"min {stats['min']}, max {stats['max']}"
                + (f", std {stats['stdev']}" if "stdev" in stats else "")
                + (f", {stats['missing']} blank" if stats.get("missing") else "")
            )
        for column, info in list(profile["categorical"].items())[:4]:
            top = ", ".join(f"{value} ({count})" for value, count in info["top"][:3])
            lines.append(f"  · {column}: {info['unique']} distinct — top {top}")
    if expression:
        expression_text, value = expression
        lines.append(f"Expression: {expression_text} = {value:g}")
    if numbers:
        stats = _stats(numbers)
        lines.append(
            f"Numbers found in the request ({len(numbers)}): sum {stats['sum']}, "
            f"mean {stats['mean']}, median {stats['median']}, min {stats['min']}, max {stats['max']}."
        )
    if not lines:
        lines.append("No numeric data was found in the request or the attachments.")
    return "\n".join(lines)


DATA_PROMPT = (
    "You are the data analyst on a small agent team. You are given figures that were "
    "already computed by code — never recompute them differently and never invent a "
    "number that is not listed. Explain what they mean for the user's request, point out "
    "the notable values or trends, and mention caveats (small sample, blanks, outliers)."
)


async def run_data(step: PlanStep, ctx: AgentContext) -> AgentOutcome:
    started = time.perf_counter()
    tables = [item for item in ctx.attachments if item.rows and len(item.rows) > 1]
    profiles = [analyse_table(item) for item in tables]
    expression = _safe_arithmetic(ctx.message)
    numbers = [value for value in re.findall(r"-?\d+(?:\.\d+)?", ctx.message)]
    numbers_f = []
    for value in numbers:
        try:
            numbers_f.append(float(value))
        except ValueError:
            continue
    computed = _data_answer(profiles, expression, numbers_f)
    data = {
        "tables": profiles,
        "expression": {"text": expression[0], "value": expression[1]} if expression else None,
        "request_numbers": _stats(numbers_f) if numbers_f else {},
        "computed": computed,
    }
    upstream_text, used = ctx.upstream_block(step)

    ctx.emit(
        "agent",
        "data",
        "Task received",
        task=step.task,
        reason=step.reason,
        from_agents=used,
        tables=[profile["file"] for profile in profiles],
        expression=expression[0] if expression else None,
    )
    ctx.emit(
        "agent",
        "data",
        "Computed the figures in code (deterministic, no model)",
        computed=computed,
        profiles=profiles,
        expression={"text": expression[0], "value": expression[1]} if expression else None,
    )

    if not ctx.has_model:
        return AgentOutcome(
            agent="data",
            status="ok",
            answer=computed,
            task=step.task,
            reason=step.reason,
            detail="computed, not narrated (no model)",
            data=data,
            latency_ms=int((time.perf_counter() - started) * 1000),
            depends_on=list(step.depends_on),
        )

    messages = [
        {"role": "system", "content": DATA_PROMPT},
        {
            "role": "user",
            "content": (
                f"Request: {ctx.message}\n\nYour task: {step.task}\n\n"
                f"Computed figures:\n{computed}"
            )
            + upstream_text,
        },
    ]
    ctx.emit_prompt("agent", "data", "Prompt sent to the model", messages, kind="data")
    try:
        response = await ctx.model_chain.complete(
            messages, None, temperature=0.1, max_tokens=min(600, ctx.settings.llm_max_tokens)
        )
    except Exception as exc:
        ctx.emit("agent", "data", "Model call failed", error=f"{type(exc).__name__}: {exc}")
        raise
    ctx.emit_response("agent", "data", "Model reply", response)
    return AgentOutcome(
        agent="data",
        status="ok",
        answer=response.content or computed,
        task=step.task,
        reason=step.reason,
        provider=response.provider,
        model=response.model,
        detail="computed + interpreted",
        data=data,
        latency_ms=int((time.perf_counter() - started) * 1000),
        model_calls=1,
        depends_on=list(step.depends_on),
    )


AGENT_RUNNERS: dict[str, Callable[[PlanStep, AgentContext], Awaitable[AgentOutcome]]] = {
    "document": run_document,
    "search": run_search,
    "data": run_data,
    "general": run_general,
}


def catalogue() -> list[dict[str, Any]]:
    return [
        {"name": name, "label": meta["label"], "description": meta["description"]}
        for name, meta in AGENTS.items()
    ]
