"""
backend.agents.general_agent
============================

**General agent** — open-domain conversation, writing, explanation and
general-knowledge questions.  This is the catch-all specialist: anything
that is not documents, live search, or quantitative analysis lands here.

Implementation is a single well-prompted LLM chat call with the recent
conversation history injected, so follow-ups ("and what about…?") work.
"""

from __future__ import annotations

from ..llm import BaseLLMClient
from ..schemas import AgentName, AgentResult
from .base import AgentContext, BaseAgent

SYSTEM_PROMPT = """You are the General Agent of a multi-agent orchestration system.

Your job: answer open-domain conversational requests — explanations, writing,
brainstorming, general knowledge, advice, definitions, translations.

Guidelines:
- Be direct, accurate and well-structured; match the user's language.
- If the request is actually about documents, live web data, or heavy
  quantitative analysis, still answer helpfully but say which specialist
  (Document / Web Search / Number agent) would do better.
- Never invent sources or citations.

Answer the user's request now."""


class GeneralAgent(BaseAgent):
    name = AgentName.GENERAL
    description = "General knowledge, conversation and writing"

    def __init__(self, llm: BaseLLMClient) -> None:
        super().__init__(llm)

    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        parts = []
        history = context.history_text()
        if history:
            parts.append(f"## CONVERSATION HISTORY\n{history}")
        upstream = context.upstream_text()
        if upstream:
            parts.append(f"## UPSTREAM FINDINGS\n{upstream}")
        parts.append(f"## USER MESSAGE\n{task}")
        prompt = "\n\n".join(parts)

        answer = await self._llm.complete_text(SYSTEM_PROMPT, prompt)
        return AgentResult(
            agent=self.name,
            success=True,
            output=answer,
            thinking="Answered as the conversational/general-knowledge specialist.",
            data={"prompt_chars": len(prompt)},
        )
