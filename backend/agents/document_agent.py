"""
backend.agents.document_agent
=============================

**Document agent** — file handling and RAG-grounded chat.

Flow
----
1. Retrieve the most relevant passages from the local
   :class:`~backend.rag.store.DocumentStore` for the task/question.
2. Ask the LLM to answer *strictly from those passages* (with history for
   multi-turn document chat).
3. Return the answer plus every retrieved chunk + score in ``data`` so
   the trace shows exactly what grounding was used.
"""

from __future__ import annotations

from ..llm import BaseLLMClient
from ..rag.store import DocumentStore
from ..schemas import AgentName, AgentResult
from .base import AgentContext, BaseAgent

SYSTEM_PROMPT = """You are the Document Agent of a multi-agent orchestration system.

You answer questions **strictly using the retrieved document passages** below.

Rules:
- Ground every claim in the passages; reference them as (doc: <name>).
- If the passages don't contain the answer, say so plainly and suggest
  what to upload or rephrase — never invent document content.
- For follow-up questions, use the conversation history to resolve
  pronouns ("it", "that section") before answering.

Answer now."""


class DocumentAgent(BaseAgent):
    name = AgentName.DOCUMENT
    description = "File handling and RAG-grounded document chat"

    def __init__(self, llm: BaseLLMClient, document_store: DocumentStore) -> None:
        super().__init__(llm)
        self._store = document_store

    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        # 1. Retrieve grounding passages from the local corpus.
        query = _question_from_task(task)
        retrieved = self._store.retrieve(query, top_k=4)

        if retrieved:
            context_block = "\n\n".join(
                f"[doc: {r.chunk.doc_name} · chunk {r.chunk.index} · score {r.score:.3f}]\n"
                f"{r.chunk.text}"
                for r in retrieved
            )
        else:
            context_block = "(no relevant passages found in the uploaded documents)"

        # 2. Grounded generation.
        parts = []
        history = context.history_text()
        if history:
            parts.append(f"## CONVERSATION HISTORY\n{history}")
        upstream = context.upstream_text()
        if upstream:
            parts.append(f"## UPSTREAM FINDINGS\n{upstream}")
        parts.append(f"## RETRIEVED CONTEXT\n{context_block}")
        parts.append(f"## USER QUESTION\n{query}")
        prompt = "\n\n".join(parts)

        answer = await self._llm.complete_text(SYSTEM_PROMPT, prompt)

        return AgentResult(
            agent=self.name,
            success=True,
            output=answer,
            thinking=(
                f"Retrieved {len(retrieved)} passage(s) from "
                f"{self._store.document_count} indexed document(s), then "
                f"generated a grounded answer."
            ),
            data={
                "question": query,
                "documents": self._store.list_documents(),
                "retrieved_chunks": [r.to_dict() for r in retrieved],
            },
        )


def _question_from_task(task: str) -> str:
    """
    Extract the user question from a Planner instruction such as
    ``Retrieve and use knowledge from uploaded documents relevant to: "…"``.
    """
    import re

    quoted = re.search(r'"([^"]+)"', task)
    if quoted:
        return quoted.group(1).strip()
    cleaned = re.sub(
        r"^(retrieve and use knowledge from uploaded documents relevant to|"
        r"answer (the )?question|use the documents to answer|look up in the documents)\b",
        "",
        task,
        flags=re.I,
    ).strip(" :.-")
    return cleaned or task
