"""
backend.llm.mock_client
=======================

Deterministic **offline** LLM used whenever no API key is configured
(or when ``LLM_PROVIDER=mock``).  It lets the entire orchestration
pipeline — Manager, Planner, parallel Executor, agents, traces — run and
be tested on a machine with no internet and no credentials.

How it works
------------
Every system prompt in this project names its author, e.g.
``"You are the Planner..."``.  The mock detects that role and produces a
realistic, keyword-driven answer:

* **Planner**  → a full JSON task plan derived from the goal's wording
* **Manager**  → analysis / synthesis text
* **Agents**   → role-flavoured answers that echo the task honestly

Real prompt templates and the mock were designed together, so swapping in
Groq later requires zero code changes above this module.
"""

from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional, Tuple

from .base import BaseLLMClient, LLMMessage, LLMResponse


# ──────────────────────────────────────────────────────────────────
#  Role detection
# ──────────────────────────────────────────────────────────────────
_ROLE_PATTERNS = [
    ("planner", re.compile(r"you are the planner", re.I)),
    ("manager", re.compile(r"you are the manager", re.I)),
    ("web_search", re.compile(r"you are the (web )?search agent", re.I)),
    ("document", re.compile(r"you are the document agent", re.I)),
    ("number", re.compile(r"you are the number agent", re.I)),
    ("general", re.compile(r"you are the general agent", re.I)),
]


def _detect_role(system_text: str) -> str:
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(system_text):
            return role
    return "generic"


def _extract_section(text: str, header: str) -> str:
    """
    Pull the body under a markdown-style ``## HEADER`` section.

    Prompt templates delimit content this way (``## USER GOAL`` etc.);
    real LLMs ignore the markup while the mock can parse it reliably.
    """
    match = re.search(
        rf"^##\s+{re.escape(header)}\s*$(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ──────────────────────────────────────────────────────────────────
#  Planner heuristics
# ──────────────────────────────────────────────────────────────────
_AGENT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "web_search": (
        "search", "web", "internet", "online", "latest", "news", "current",
        "today", "price", "weather", "look up", "lookup", "find out",
        "google", "recent", "trend", "who is", "what's new", "research",
        "competitor", "market",
    ),
    "document": (
        "document", "file", "pdf", "upload", "rag", "according to",
        "based on the document", "my notes", "policy", "contract",
        "knowledge base", "the doc", "attached", "corpus", "handbook",
        "manual", "transcript",
    ),
    "number": (
        "data", "analy", "number", "statistic", "calculat", "math",
        "compute", "metric", "dataset", "table", "compare", "forecast",
        "budget", "revenue", "percent", "chart", "visuali", "complex",
        "quantit", "sum of", "average", "kpi", "spreadsheet",
    ),
    "general": (
        "explain", "write", "summar", "translate", "brainstorm", "advice",
        "how to", "why", "story", "email", "draft", "idea", "describe",
        "define", "help me", "suggest",
    ),
}


def _select_agents(goal: str) -> List[str]:
    """Keyword → agent routing used by the mock Planner."""
    lower = goal.lower()
    selected: List[str] = []
    for agent, keywords in _AGENT_KEYWORDS.items():
        if any(keyword in lower for keyword in keywords):
            selected.append(agent)

    # Nothing matched → default to the conversational agent.
    if not selected:
        return ["general"]

    # Pure chit-chat shouldn't spawn specialist agents even if a stray
    # keyword slipped in — but only when the goal is very short.
    if len(goal.split()) <= 4 and "general" in selected:
        return ["general"]

    # If general matched but specialists did too, drop general unless the
    # goal clearly asks for writing/explaining on top of the specialist work.
    if "general" in selected and len(selected) > 1:
        writing_words = ("explain", "write", "summar", "draft", "report", "story", "email")
        if not any(w in lower for w in writing_words):
            selected.remove("general")

    # Preserve stable ordering: web → document → number → general.
    order = ["web_search", "document", "number", "general"]
    return [a for a in order if a in selected]


def _build_plan(goal: str) -> dict:
    """
    Produce a dependency-aware task plan, mirroring what a real Planner
    LLM is instructed to return.
    """
    agents = _select_agents(goal)
    lower = goal.lower()

    analysis = (
        f"The goal mixes {len(agents)} concern(s): "
        + ", ".join(agents)
        + ". Each concern maps to one specialist agent; independent tasks "
        "can run in parallel."
    )

    templates = {
        "web_search": f"Search the web for up-to-date information relevant to: \"{goal}\"",
        "document": f"Retrieve and use knowledge from uploaded documents relevant to: \"{goal}\"",
        "number": f"Perform quantitative / data analysis for: \"{goal}\"",
        "general": f"Answer conversationally and cover general knowledge for: \"{goal}\"",
    }

    tasks: List[dict] = []
    for idx, agent in enumerate(agents, start=1):
        tasks.append(
            {
                "id": f"t{idx}",
                "agent": agent,
                "description": templates[agent],
                "depends_on": [],
                "reason": f"Goal wording indicates '{agent}' work is required.",
            }
        )

    # Dependency heuristic: analysis/reporting over other agents' findings
    # should wait for them (demonstrates the executor's wave scheduling).
    analysis_words = ("analy", "compare", "report", "insight", "summar", "recommend")
    if "number" in agents and any(w in lower for w in analysis_words):
        upstream = [t["id"] for t in tasks if t["agent"] in ("web_search", "document")]
        for t in tasks:
            if t["agent"] == "number":
                t["depends_on"] = upstream
                t["description"] = (
                    f"Using the findings of the upstream tasks, perform "
                    f"quantitative / data analysis for: \"{goal}\""
                )
                t["reason"] = "Analysis quality improves when it can consume upstream findings."

    return {"analysis": analysis, "tasks": tasks}


# ──────────────────────────────────────────────────────────────────
#  Per-role response builders
# ──────────────────────────────────────────────────────────────────
def _planner_response(user_text: str, json_mode: bool) -> str:
    goal = _extract_section(user_text, "USER GOAL") or user_text.strip()
    plan = _build_plan(goal)
    return json.dumps(plan, indent=2)


def _manager_response(user_text: str) -> str:
    goal = _extract_section(user_text, "USER GOAL") or user_text.strip()
    results_block = _extract_section(user_text, "TASK RESULTS")

    if results_block:
        # Synthesis request: weave task outputs into a final answer.
        lines = [ln for ln in results_block.splitlines() if ln.strip()]
        agent_hits = re.findall(r"###\s+task\s+\S+\s+\((\w+)\)", results_block, re.I)
        agents_used = ", ".join(sorted(set(agent_hits))) or "the agent team"
        excerpts = re.findall(r"(?:Output|Result)\s*:\s*(.+)", results_block, re.I)
        if not excerpts:
            excerpts = [ln.strip() for ln in lines if not ln.startswith("#")][:3]
        summary = " ".join(e[:220] for e in excerpts[:3])
        return (
            f"[Manager synthesis — offline mock]\n\n"
            f"Goal: {goal}\n\n"
            f"I delegated this goal to: {agents_used}. "
            f"Combining their findings: {summary}\n\n"
            f"Conclusion: the goal was addressed by coordinating the specialist "
            f"agents above; each result is preserved in the trace for evaluation."
        )

    # Analysis / thinking request.
    agents = _select_agents(goal)
    return (
        f"[Manager analysis — offline mock]\n\n"
        f"Goal understood: \"{goal}\".\n"
        f"The goal requires involvement of: {', '.join(agents)}.\n"
        f"Strategy: ask the Planner for a dependency-aware task plan, execute "
        f"independent tasks in parallel, then synthesize a single answer.\n"
        f"Success criteria: every relevant concern of the goal is covered by a "
        f"specialist agent and the final answer is coherent and complete."
    )


def _search_agent_response(user_text: str) -> str:
    query = _extract_section(user_text, "SEARCH QUERY") or _extract_section(user_text, "INPUT")
    results = _extract_section(user_text, "SEARCH RESULTS")
    pages = _extract_section(user_text, "PAGE CONTENT")
    if not query:
        query = user_text.strip()[:200]
    if results:
        snippets = [
            ln.strip(" -•\t")
            for ln in results.splitlines()
            if ln.strip() and not ln.strip().startswith(("###", "##", "#"))
        ][:3]
        body = " ".join(s[:160] for s in snippets)
        crawl_note = (
            f" Full crawled page content was also used as grounding "
            f"({len(pages.split('[page')) - 1} page(s))."
            if pages
            else ""
        )
        return (
            f"[Web Search Agent — offline mock synthesis]\n\n"
            f"Query: \"{query}\"\n\n"
            f"Based on the retrieved search results: {body}.{crawl_note}\n\n"
            f"Sources are listed in the task's structured data (simulated provider)."
        )
    return (
        f"[Web Search Agent — offline mock]\n\n"
        f"No search results were available for \"{query}\". "
        f"Configure WEB_SEARCH_PROVIDER=searxng in .env for live results."
    )


def _document_agent_response(user_text: str) -> str:
    question = (
        _extract_section(user_text, "USER QUESTION")
        or _extract_section(user_text, "INPUT")
        or user_text.strip()[:200]
    )
    context = _extract_section(user_text, "RETRIEVED CONTEXT") or _extract_section(user_text, "CONTEXT")
    if context:
        excerpt = context.strip()[:400]
        return (
            f"[Document Agent — offline mock answer]\n\n"
            f"Question: \"{question}\"\n\n"
            f"According to the retrieved document passages: {excerpt} …\n\n"
            f"(Answer grounded in the local RAG store; exact passages are kept "
            f"in the trace.)"
        )
    return (
        f"[Document Agent — offline mock]\n\n"
        f"I could not find relevant passages in the uploaded documents for "
        f"\"{question}\". Upload documents via the test UI, then retry."
    )


def _number_agent_response(user_text: str) -> str:
    data = _extract_section(user_text, "INPUT") or user_text.strip()[:200]
    upstream = _extract_section(user_text, "UPSTREAM FINDINGS")
    upstream_note = (
        " Incorporating the upstream task findings into the analysis."
        if upstream
        else ""
    )
    return (
        f"[Number Agent — offline mock analysis]\n\n"
        f"Task: \"{data}\".{upstream_note}\n\n"
        f"Approach: break the quantitative problem into components, compute "
        f"the relevant aggregates, and summarise the numbers with their "
        f"implications. All computed values and reasoning appear in the trace "
        f"for evaluation."
    )


def _general_agent_response(user_text: str) -> str:
    message = _extract_section(user_text, "USER MESSAGE") or _extract_section(user_text, "INPUT") or user_text.strip()
    return (
        f"[General Agent — offline mock]\n\n"
        f"You asked: \"{message[:300]}\"\n\n"
        f"Here is a helpful conversational answer drawing on general "
        f"knowledge: I understand the request and would respond in a natural, "
        f"informative way. With GROQ_API_KEY configured this answer is "
        f"generated by a real LLM instead of this offline mock."
    )


def _generic_response(user_text: str) -> str:
    return (
        "[Offline mock LLM]\n\n"
        "This is a deterministic offline response. Configure GROQ_API_KEY "
        f"in .env for real LLM output. Your prompt started with: "
        f"\"{user_text.strip()[:160]}\""
    )


def _build_content(role: str, system_text: str, user_text: str, json_mode: bool) -> str:
    if role == "planner":
        return _planner_response(user_text, json_mode)
    if role == "manager":
        return _manager_response(user_text)
    if role == "web_search":
        return _search_agent_response(user_text)
    if role == "document":
        return _document_agent_response(user_text)
    if role == "number":
        return _number_agent_response(user_text)
    if role == "general":
        return _general_agent_response(user_text)

    content = _generic_response(user_text)
    if json_mode:
        return json.dumps({"response": content})
    return content


# ──────────────────────────────────────────────────────────────────
#  Client
# ──────────────────────────────────────────────────────────────────
class MockLLMClient(BaseLLMClient):
    """
    Deterministic stand-in for a real chat model.

    Responses are stable for identical inputs (great for tests) and each
    response is clearly labelled as offline-mock output so evaluators can
    never confuse it with real model text.
    """

    provider = "mock"

    def __init__(self, model: str = "mock-llm") -> None:
        self._model = model

    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        started = time.perf_counter()
        system_text = "\n".join(m.content for m in messages if m.role == "system")
        user_text = "\n".join(m.content for m in messages if m.role == "user")

        role = _detect_role(system_text)
        content = _build_content(role, system_text, user_text, json_mode)

        if json_mode and role == "planner":
            # Already JSON — validate it parses so failures surface here.
            json.loads(content)

        return LLMResponse(
            content=content,
            model=self._model,
            usage={
                "prompt_tokens": _estimate_tokens(system_text + user_text),
                "completion_tokens": _estimate_tokens(content),
                "total_tokens": _estimate_tokens(system_text + user_text + content),
            },
            latency_ms=(time.perf_counter() - started) * 1000,
            provider=self.provider,
            mocked=True,
        )
