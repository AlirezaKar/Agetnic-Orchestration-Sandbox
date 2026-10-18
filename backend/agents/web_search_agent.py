"""
backend.agents/web_search_agent
===============================

**Web-search agent** — finds fresh information on the web and answers
grounded in the results.

Flow
----
1. Derive a focused search query from the task instruction.
2. Call the configured :class:`SearchProvider` (simulated/tavily/serper).
3. Ask the LLM to synthesise an answer *strictly from those results*,
   keeping the sources in the structured ``data`` payload for the trace.
"""

from __future__ import annotations

from ..llm import BaseLLMClient
from ..schemas import AgentName, AgentResult
from ..search import SearchProvider
from .base import AgentContext, BaseAgent

SYSTEM_PROMPT = """You are the Web Search Agent of a multi-agent orchestration system.

You receive a task and a list of web search results (title / url / snippet).
Write a concise, factual answer **grounded strictly in those results**.

Rules:
- Cite sources inline as [1], [2]… matching the numbered results.
- If the results are simulated (sandbox/offline mode), say so briefly at
  the end so evaluators know live web data was not used.
- If the results don't cover the question, say what is missing instead of
  inventing facts.

Answer now."""


class WebSearchAgent(BaseAgent):
    name = AgentName.WEB_SEARCH
    description = "Live web search + grounded summarisation"

    def __init__(self, llm: BaseLLMClient, search_provider: SearchProvider) -> None:
        super().__init__(llm)
        self._search = search_provider

    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        # 1. Search query = the task itself (the Planner phrases it as a
        #    search instruction; trimming the boilerplate keeps it clean).
        query = _query_from_task(task)

        # 2. Retrieve results from the configured provider.
        search_response = await self._search.search(query, max_results=5)

        # 3. Synthesise a grounded answer.
        numbered = "\n".join(
            f"{i}. {r.title} — {r.url}\n   {r.snippet}"
            for i, r in enumerate(search_response.results, start=1)
        )
        prompt = (
            f"## SEARCH QUERY\n{query}\n\n"
            f"## SEARCH RESULTS\n{numbered or '(no results returned)'}\n"
        )
        answer = await self._llm.complete_text(SYSTEM_PROMPT, prompt)

        return AgentResult(
            agent=self.name,
            success=True,
            output=answer,
            thinking=(
                f"Searched with provider '{search_response.provider}' "
                f"({'simulated' if search_response.simulated else 'live'}) "
                f"and synthesised an answer grounded in "
                f"{len(search_response.results)} result(s)."
            ),
            data={
                "query": query,
                "search": search_response.to_dict(),
            },
        )


def _query_from_task(task: str) -> str:
    """
    Turn a Planner instruction into a clean search query.

    The Planner writes instructions like
    ``Search the web for up-to-date information relevant to: "…"`` —
    we extract the quoted core when present, otherwise strip common
    imperative prefixes.
    """
    import re

    quoted = re.search(r'"([^"]+)"', task)
    if quoted:
        return quoted.group(1).strip()
    cleaned = re.sub(
        r"^(search the web for|search for|look up|find|research)\b",
        "",
        task,
        flags=re.I,
    ).strip(" :.-")
    return cleaned or task
