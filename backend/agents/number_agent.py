"""
backend.agents.number_agent
===========================

**Number agent** — data & complex information analysis: statistics,
math, comparisons, KPIs, structured reasoning over numbers.

Two-stage implementation:

1. **Arithmetic tool** — obvious numeric expressions are computed locally
   with a *safe* AST-based evaluator (no ``eval``) so pure-math questions
   get exact answers even with the offline mock LLM.
2. **LLM analysis** — the analytical reasoning/writing step, with any
   upstream findings (e.g. search results) injected for integrated
   analysis.
"""

from __future__ import annotations

import ast
import operator
import re
from typing import Optional

from ..llm import BaseLLMClient
from ..schemas import AgentName, AgentResult
from .base import AgentContext, BaseAgent

SYSTEM_PROMPT = """You are the Number Agent of a multi-agent orchestration system.

Your job: quantitative reasoning and data/complex-information analysis —
arithmetic, statistics, KPIs, comparisons, forecasts, structured numeric
interpretation.

Guidelines:
- Show the method: state given numbers → compute/compare → interpret.
- If upstream findings (search snippets, document passages) contain
  numbers, use them; flag any assumption you make.
- Round sensibly and include units. Prefer concrete numbers over vague
  adjectives.

Provide the analysis now."""

# ──────────────────────────────────────────────────────────────────
#  Safe arithmetic tool (no eval, no names, arithmetic ops only)
# ──────────────────────────────────────────────────────────────────
_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Recognise an inline arithmetic expression such as "12.5*(3+1)/2".
_EXPR_RE = re.compile(r"^[0-9\s\.\+\-\*/\(\)%]+$")


def safe_calculate(expression: str) -> Optional[float]:
    """
    Evaluate a plain arithmetic expression safely.

    Returns ``None`` when the expression contains anything beyond numbers
    and + - * / % ** ( ) — i.e. it can never execute arbitrary code.
    """
    expression = expression.strip().rstrip("=? ")
    if not expression or not _EXPR_RE.match(expression):
        return None
    try:
        node = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None

    def _eval(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return _eval(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow):
            # Guard: exponents explode memory (2**10**12 is petabytes).
            left, right = _eval(n.left), _eval(n.right)
            if abs(right) > 12 or abs(left) > 1e9:
                raise ValueError("exponentiation out of safe range")
            return left ** right
        if isinstance(n, ast.BinOp) and type(n.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(n.op)](_eval(n.left), _eval(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(n.op)](_eval(n.operand))
        raise ValueError("unsupported expression element")

    try:
        return _eval(node)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def extract_expression(text: str) -> Optional[str]:
    """Find the first arithmetic-looking expression inside free text."""
    for match in re.finditer(r"[0-9\.\s\(\)]+(?:[\+\-\*/\^%][0-9\.\s\(\)]+)+", text):
        candidate = match.group(0).strip().replace("^", "**")
        if safe_calculate(candidate) is not None:
            return candidate
    return None


class NumberAgent(BaseAgent):
    name = AgentName.NUMBER
    description = "Data, statistics, math and complex quantitative analysis"

    def __init__(self, llm: BaseLLMClient) -> None:
        super().__init__(llm)

    async def _execute(self, task: str, context: AgentContext) -> AgentResult:
        # ── Stage 1: local arithmetic tool ──────────────────────
        tool_note = ""
        expression = extract_expression(task)
        if expression:
            value = safe_calculate(expression)
            if value is not None:
                tool_note = f"Computed locally (safe arithmetic tool): {expression} = {value}"

        # ── Stage 2: LLM analysis ──────────────────────────────
        parts = []
        if tool_note:
            parts.append(f"## TOOL RESULT\n{tool_note}")
        upstream = context.upstream_text()
        if upstream:
            parts.append(f"## UPSTREAM FINDINGS\n{upstream}")
        parts.append(f"## INPUT\n{task}")
        prompt = "\n\n".join(parts)

        analysis = await self._llm.complete_text(SYSTEM_PROMPT, prompt)
        output = f"{tool_note}\n\n{analysis}".strip() if tool_note else analysis

        return AgentResult(
            agent=self.name,
            success=True,
            output=output,
            thinking=(
                "Ran the local arithmetic tool for exact numeric expressions, "
                "then produced the analytical reasoning with the LLM."
            ),
            data={"computed_expression": expression, "tool_note": tool_note},
        )
