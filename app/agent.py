"""The orchestration loop: model <-> tools, with a full trace.

The model decides when to search. Every tool call is executed against the
configured search chain, the result is fed back, and the loop stops when the
model answers without requesting another tool.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.llm.base import ModelResponse, ProviderError
from app.search.base import SearchError
from app.search.fetch import fetch_page

SYSTEM_PROMPT = (
    "You are the orchestration core of a local sandbox. You can call tools.\n"
    "Use `web_search` whenever the answer depends on current or verifiable facts, "
    "and `fetch_url` when a snippet is not enough. Prefer primary sources.\n"
    "Cite the URLs you used inline, like [1] https://... .\n"
    "If a tool fails, say what failed instead of inventing results. "
    "If you did not search, do not imply that you did."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web. Returns titles, URLs and snippets. "
                "Use for news, docs, prices, releases, anything time-sensitive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "How many results to return (1-10).",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL and return its readable text (truncated).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Absolute http(s) URL."}},
                "required": ["url"],
            },
        },
    },
]


@dataclass
class AgentResult:
    answer: str
    provider: str = ""
    model: str = ""
    steps: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    search_provider: str = ""
    stopped_reason: str = "answered"
    latency_ms: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "provider": self.provider,
            "model": self.model,
            "steps": self.steps,
            "trace": self.trace,
            "sources": self.sources,
            "usage": self.usage,
            "search_provider": self.search_provider,
            "stopped_reason": self.stopped_reason,
            "latency_ms": self.latency_ms,
            "errors": self.errors,
        }


class WebSearchAgent:
    """One model, two tools (``web_search``, ``fetch_url``), fully traced.

    ``observer`` is an optional callable the orchestrator passes in to see the
    loop from the inside:

    * ``observer("prompt", {"messages": [...]})`` before every model call
    * ``observer("model", {"response": ModelResponse, "step": int})`` after it
    * ``observer("tool", {"call": ToolCall, "output": dict, "latency_ms": int})``

    It is deliberately dumb: it only forwards data, it never decides anything.
    """

    def __init__(
        self,
        model_chain: Any,
        search_chain: Any,
        settings: Settings,
        observer: Any = None,
    ):
        self.model_chain = model_chain
        self.search_chain = search_chain
        self.settings = settings
        self.observer = observer

    def _observe(self, kind: str, payload: dict[str, Any]) -> None:
        if self.observer is None:
            return
        try:
            self.observer(kind, payload)
        except Exception:  # pragma: no cover - tracing must never break the run
            pass

    # ------------------------------------------------------------- tooling
    async def _run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "web_search":
            query = str(arguments.get("query") or "").strip()
            if not query:
                return {"ok": False, "error": "web_search requires a non-empty 'query'"}
            requested = arguments.get("max_results") or self.settings.agent_max_search_results
            try:
                limit = max(1, min(int(requested), 10))
            except (TypeError, ValueError):
                limit = self.settings.agent_max_search_results
            try:
                response = await self.search_chain.search(query, limit)
            except SearchError as exc:
                return {"ok": False, "error": exc.to_dict()}
            return {
                "ok": True,
                "provider": response.provider,
                "query": response.query,
                "results": [r.as_tool_output() for r in response.results],
            }

        if name == "fetch_url":
            url = str(arguments.get("url") or "").strip()
            if not url:
                return {"ok": False, "error": "fetch_url requires a 'url'"}
            try:
                page = await fetch_page(url, self.settings)
            except SearchError as exc:
                return {"ok": False, "error": exc.to_dict()}
            return {"ok": True, **page}

        return {"ok": False, "error": f"unknown tool '{name}'"}

    # ---------------------------------------------------------------- loop
    async def run(
        self,
        message: str,
        *,
        history: list[dict[str, Any]] | None = None,
        max_steps: int | None = None,
        allow_search: bool = True,
    ) -> AgentResult:
        started = time.perf_counter()
        max_steps = max_steps or self.settings.agent_max_steps
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in history or []:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                messages.append(
                    {"role": item["role"], "content": str(item.get("content", ""))[:8000]}
                )
        messages.append({"role": "user", "content": message})

        tools = TOOLS if allow_search else None
        trace: list[dict[str, Any]] = []
        sources: list[str] = []
        errors: list[dict[str, Any]] = []
        search_provider = ""
        provider = model = ""
        usage: dict[str, Any] = {}
        stopped = "max_steps"

        for step in range(1, max_steps + 1):
            self._observe("prompt", {"messages": [dict(m) for m in messages], "step": step})
            response: ModelResponse = await self.model_chain.complete(
                messages,
                tools,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
            )
            provider, model = response.provider, response.model
            usage = response.usage or usage
            self._observe("model", {"response": response, "step": step})
            trace.append(
                {
                    "step": step,
                    "type": "model",
                    "provider": response.provider,
                    "model": response.model,
                    "content": response.content[:800],
                    "tool_calls": [
                        {"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls
                    ],
                    "latency_ms": response.latency_ms,
                    "finish_reason": response.finish_reason,
                }
            )

            if not response.tool_calls:
                stopped = "answered"
                return AgentResult(
                    answer=response.content,
                    provider=provider,
                    model=model,
                    steps=step,
                    trace=trace,
                    sources=sources,
                    usage=usage,
                    search_provider=search_provider,
                    stopped_reason=stopped,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    errors=errors,
                )

            messages.append(response.to_assistant_message())
            for call in response.tool_calls:
                tool_started = time.perf_counter()
                try:
                    output = await self._run_tool(call.name, call.arguments)
                except Exception as exc:  # defensive: never kill the loop
                    output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                elapsed = int((time.perf_counter() - tool_started) * 1000)
                self._observe(
                    "tool",
                    {"call": call, "output": output, "latency_ms": elapsed, "step": step},
                )

                if call.name == "web_search" and output.get("ok"):
                    search_provider = output.get("provider") or search_provider
                    for item in output.get("results", []):
                        url = item.get("url")
                        if url and url not in sources:
                            sources.append(url)
                if not output.get("ok"):
                    errors.append({"tool": call.name, "error": output.get("error")})

                trace.append(
                    {
                        "step": step,
                        "type": "tool",
                        "name": call.name,
                        "arguments": call.arguments,
                        "ok": bool(output.get("ok")),
                        "latency_ms": elapsed,
                        "preview": _preview(output),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(output, ensure_ascii=False)[:12000],
                    }
                )

        # ran out of steps: ask once more without tools for a final summary
        try:
            final_messages = messages + [
                {"role": "user", "content": "Stop calling tools. Answer now with what you have."}
            ]
            self._observe("prompt", {"messages": [dict(m) for m in final_messages], "step": max_steps + 1})
            final = await self.model_chain.complete(
                final_messages,
                None,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
            )
            answer = final.content
            provider, model = final.provider, final.model
            usage = final.usage or usage
        except ProviderError as exc:
            answer = ""
            errors.append(exc.to_dict())

        return AgentResult(
            answer=answer,
            provider=provider,
            model=model,
            steps=max_steps,
            trace=trace,
            sources=sources,
            usage=usage,
            search_provider=search_provider,
            stopped_reason=stopped,
            latency_ms=int((time.perf_counter() - started) * 1000),
            errors=errors,
        )


def _preview(output: dict[str, Any], limit: int = 400) -> Any:
    if output.get("ok") and "results" in output:
        return [
            {"title": r.get("title", "")[:120], "url": r.get("url", "")}
            for r in output["results"][:5]
        ]
    if output.get("ok") and "content" in output:
        return str(output["content"])[:limit]
    return output.get("error")
