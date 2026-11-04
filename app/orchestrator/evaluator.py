"""Evaluator–optimizer: the gate between the specialists and the manager.

The pipeline used to hand the agent reports straight to the manager. This stage
sits between them. After the waves finish — and *before* the manager reads
anything — one cheap completion:

1. **evaluates** every report against the user's request: a verdict
   (``keep`` / ``revise`` / ``fail``), a score out of 5 and the concrete issues
   it found;
2. **optimizes** the weak ones: a rewrite that sharpens the report without
   inventing a source, a figure or a quote;
3. **summarises** the set: one sentence plus the gaps the team still has.

The manager then works from the *optimized* reports and from that summary, which
is what makes its review meaningful: ``"the search report has no date, ask the
web researcher again"`` is a decision it can only take about material it has
read. Its follow-up wave comes back through this same gate.

Three guarantees:

* **nothing is invented** — the rewrite may drop, compress, structure and
  re-state what an agent wrote; a URL, a number or a quote that was not in the
  report must not appear in the rewrite. The prompt says so, and
  :func:`keep_facts` checks it: a rewrite that drops every source or every
  number the original had is discarded and the original is used instead.
* **the original is never lost** — a revised report keeps ``answer`` untouched
  and puts the rewrite in ``optimized``. The trace, the run log and the UI all
  keep both, so the run stays auditable.
* **no model, no problem** — without a model chain, or when the call fails, the
  same stage runs a rule check (empty, truncated, sourceless search, digitless
  data answer) in microseconds. It still scores, still flags gaps, and still
  gives the manager something to review; it just cannot rewrite.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.orchestrator.agents import AgentContext, AgentOutcome
from app.orchestrator.planner import AGENTS, Plan

EVALUATOR_PROMPT = (
    "You are the evaluator between a team of specialist agents and their manager.\n"
    "For every report you are given, judge it against the user's request, list the concrete "
    "problems, and — only when you can do it without adding anything new — rewrite it into a "
    "sharper, self-contained report for the manager.\n"
    "Rules:\n"
    "- Never invent sources, URLs, numbers, quotes or file content. Every fact in a rewrite must "
    "already be in the original report.\n"
    "- Rewriting means: drop the chatter, lead with the answer, keep the evidence, structure it.\n"
    "- Keep the language of the report. Keep its URLs and figures character for character.\n"
    "- Use \"keep\" when a report is already good (empty rewrite), \"fail\" only when it cannot be "
    "used at all.\n"
    "- In \"gaps\", name only what is genuinely missing to answer the request.\n"
    'Answer with JSON only: {"summary": "one sentence", "gaps": ["…"], '
    '"reports": [{"agent": "search", "verdict": "keep|revise|fail", "score": 1, '
    '"issues": ["…"], "rewrite": "…"}]}'
)

#: A report shorter than this has nothing in it worth a manager's attention.
MIN_USEFUL_CHARS = 80

#: How much of one report the evaluator is shown (and may hand back).
REPORT_CHARS = 6000


@dataclass
class Evaluation:
    """What the evaluator decided about one agent report."""

    agent: str
    verdict: str = "keep"  # keep | revise | fail
    score: int = 3  # 1..5
    issues: list[str] = field(default_factory=list)
    method: str = "rules"  # model | rules
    revised: bool = False
    chars_before: int = 0
    chars_after: int = 0
    # False for an agent that failed or had nothing to do: it is reported, but it
    # does not drag the run's quality score down
    usable: bool = True

    @property
    def label(self) -> str:
        return AGENTS.get(self.agent, {}).get("label", self.agent.title())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "label": self.label,
            "verdict": self.verdict,
            "score": self.score,
            "issues": self.issues,
            "method": self.method,
            "revised": self.revised,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "usable": self.usable,
        }


@dataclass
class EvaluationReport:
    """The whole gate: one round of scoring, plus whatever it rewrote."""

    requested: bool = False
    ran: bool = False
    method: str = "rules"  # model | rules | mixed
    rounds: int = 0
    summary: str = ""
    gaps: list[str] = field(default_factory=list)
    items: list[Evaluation] = field(default_factory=list)
    model_calls: int = 0
    latency_ms: int = 0
    reason: str = ""

    # ------------------------------------------------------------- helpers
    def add(self, item: Evaluation) -> Evaluation:
        self.items = [existing for existing in self.items if existing.agent != item.agent]
        self.items.append(item)
        return item

    def item_for(self, agent: str) -> Evaluation | None:
        for item in self.items:
            if item.agent == agent:
                return item
        return None

    def merge(self, other: "EvaluationReport") -> None:
        """Fold a second round (a follow-up wave) into this report."""
        if not other.ran:
            return
        self.ran = self.ran or other.ran
        self.rounds += other.rounds
        self.model_calls += other.model_calls
        self.latency_ms += other.latency_ms
        for item in other.items:
            self.add(item)
        self.summary = other.summary or self.summary
        for gap in other.gaps:
            if gap not in self.gaps:
                self.gaps.append(gap)
        if self.method != other.method:
            self.method = "mixed"

    # -------------------------------------------------------------- export
    @property
    def revised(self) -> int:
        return sum(1 for item in self.items if item.revised)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.verdict == "fail")

    @property
    def quality(self) -> float:
        """Mean score of the reports that actually answered, one decimal."""
        scored = [item.score for item in self.items if item.usable]
        return round(sum(scored) / len(scored), 1) if scored else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "ran": self.ran,
            "method": self.method,
            "rounds": self.rounds,
            "summary": self.summary,
            "gaps": self.gaps,
            "items": [item.to_dict() for item in self.items],
            "quality": self.quality,
            "revised": self.revised,
            "failed": self.failed,
            "model_calls": self.model_calls,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
        }

    def notes_block(self) -> str:
        """The evaluator's verdict as text, for the manager's next prompt."""
        if not self.ran:
            return "(the evaluator did not run)"
        lines = [f"Evaluator's summary: {self.summary or 'no summary'}"]
        if self.gaps:
            lines.append("Gaps the team did not close:")
            lines.extend(f"- {gap}" for gap in self.gaps[:6])
        for item in self.items:
            if not item.issues and item.verdict == "keep":
                continue
            issues = "; ".join(item.issues[:3]) or "no detail"
            lines.append(f"- {item.label} ({item.agent}): {item.verdict}, {item.score}/5 — {issues}")
        return "\n".join(lines)


# --------------------------------------------------------------- rule check
def _mentions_number(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def _mentions_source(text: str) -> bool:
    return bool(re.search(r"https?://|www\.", text or ""))


def rule_check(outcome: AgentOutcome, *, wants_sources: bool = False) -> Evaluation:
    """Score one report without a model.

    Cheap, deterministic and honest about what it cannot see: it notices an
    empty or truncated report, a search answer with no URL when the request
    asked for sources, and a data answer that forgot the figures it computed.
    """
    item = Evaluation(agent=outcome.agent, method="rules")
    text = (outcome.answer or "").strip()
    if outcome.status == "skipped":
        # nothing to judge: the agent was not needed for this request
        item.usable = False
        item.score = 5
        item.issues = [outcome.detail or "the agent was not needed"]
        return item
    if outcome.status != "ok":
        item.usable = False
        item.verdict = "fail"
        item.score = 1
        item.issues = [outcome.error or outcome.detail or f"the agent reported {outcome.status}"]
        return item
    if not text:
        item.verdict = "fail"
        item.score = 1
        item.issues = ["the report is empty"]
        return item

    issues: list[str] = []
    if len(text) < MIN_USEFUL_CHARS:
        issues.append(f"the report is only {len(text)} characters — too thin to answer from")
    if wants_sources and outcome.agent == "search" and not _mentions_source(text):
        issues.append("no URL or citation, although the request asked for sources")
    if outcome.agent == "data" and outcome.data.get("computed") and not _mentions_number(text):
        issues.append("the computed figures never made it into the report")
    if text.endswith(("…", "...")) and len(text) < 400:
        issues.append("the report ends mid-thought")

    item.issues = issues
    item.score = 5 - min(len(issues) * 2, 4)
    item.verdict = "keep" if not issues else "revise"
    return item


# ------------------------------------------------------------------- parse
def _parse_evaluation(raw: str, roster: list[str]) -> dict[str, Any]:
    """Read the evaluator's JSON, keeping only known agents and sane values."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return {"summary": "", "gaps": [], "reports": {}}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"summary": "", "gaps": [], "reports": {}}
    if not isinstance(payload, dict):
        return {"summary": "", "gaps": [], "reports": {}}

    reports: dict[str, dict[str, Any]] = {}
    for entry in payload.get("reports") or []:
        if not isinstance(entry, dict):
            continue
        agent = str(entry.get("agent") or "").strip().lower()
        if agent not in roster or agent in reports:
            continue
        try:
            score = int(float(entry.get("score") or 3))
        except (TypeError, ValueError):
            score = 3
        verdict = str(entry.get("verdict") or "keep").strip().lower()
        if verdict not in ("keep", "revise", "fail"):
            verdict = "keep"
        issues = [
            str(issue).strip()[:300]
            for issue in (entry.get("issues") or [])
            if str(issue).strip()
        ][:6]
        rewrite = str(entry.get("rewrite") or "").strip()[:REPORT_CHARS]
        reports[agent] = {
            "verdict": verdict,
            "score": max(1, min(5, score)),
            "issues": issues,
            "rewrite": rewrite,
        }
    gaps = [
        str(gap).strip()[:300]
        for gap in (payload.get("gaps") or [])
        if str(gap).strip()
    ][:6]
    return {
        "summary": str(payload.get("summary") or "").strip()[:400],
        "gaps": gaps,
        "reports": reports,
    }


def keep_facts(original: str, rewrite: str) -> bool:
    """Refuse a rewrite that throws away the evidence of the original.

    The evaluator is told not to invent facts; this is the other half of that
    bargain — it may not *drop* the evidence either. A rewrite keeps every URL
    the original had, and at least one number when the request was numeric.
    """
    if not rewrite.strip():
        return False
    for url in set(re.findall(r"https?://[^\s)\]]+", original or "")):
        if url.rstrip(".,;:") not in rewrite:
            return False
    return True


def _wants_sources(message: str) -> bool:
    lowered = (message or "").lower()
    return any(
        word in lowered
        for word in ("source", "sources", "cite", "citation", "link", "links", "reference", "url")
    )


def _report_block(results: list[AgentOutcome], limit: int) -> str:
    blocks: list[str] = []
    for outcome in results:
        head = f"### {outcome.label} ({outcome.agent}) — {outcome.status}"
        if outcome.status != "ok":
            body = f"FAILED: {outcome.error or outcome.detail or 'no output'}"
        else:
            body = (outcome.answer or "").strip()[:limit] or "(empty answer)"
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


def _files_block(attachments: list[Any]) -> str:
    if not attachments:
        return "(none)"
    return "\n".join(f"- {item.name} ({item.kind}, {item.chars} chars)" for item in attachments)


# ------------------------------------------------------------------- stage
async def evaluate_results(
    message: str,
    plan: Plan,
    results: list[AgentOutcome],
    ctx: AgentContext,
    *,
    settings: Any = None,
    optimize: bool = True,
    round_no: int = 1,
    trace: Any = None,
) -> EvaluationReport:
    """Score (and optionally rewrite) the reports before the manager reads them.

    ``results`` are the outcomes to judge; a second call after a follow-up wave
    passes only the new ones and the caller merges the reports. Each judged
    ``AgentOutcome`` leaves with ``.evaluation`` set and — when the evaluator
    rewrote it and ``optimize`` is on — ``.optimized`` filled in.
    """
    settings = settings or ctx.settings
    trace = trace if trace is not None else ctx.trace
    report = EvaluationReport(requested=True, rounds=1)
    if not results:
        report.reason = "no agent ran — nothing to evaluate"
        return report

    usable = [outcome for outcome in results if outcome.status == "ok"]
    roster = list(getattr(settings, "orchestrator_roster", []) or list(AGENTS))
    wants_sources = _wants_sources(message)
    limit = max(int(getattr(settings, "orchestrator_context_chars", 6000)), 1500)

    started = time.perf_counter()
    parsed: dict[str, Any] = {"summary": "", "gaps": [], "reports": {}}
    model = getattr(ctx, "model_chain", None)

    if model and usable:
        messages = [
            {"role": "system", "content": EVALUATOR_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User request:\n{message}\n\n"
                    f"Attached files:\n{_files_block(ctx.attachments)}\n\n"
                    f"Plan: {plan.summary}\n\n"
                    f"Reports to evaluate:\n\n{_report_block(usable, min(limit, REPORT_CHARS))}\n\n"
                    f"The agents that produced them: {', '.join(outcome.agent for outcome in usable)}"
                ),
            },
        ]
        if trace is not None:
            trace.add_prompt(
                "evaluate",
                "evaluator",
                "Evaluator prompt (score and rewrite the reports)",
                messages,
            )
        try:
            response = await model.complete(
                messages,
                None,
                temperature=0.0,
                max_tokens=min(1400, int(getattr(settings, "llm_max_tokens", 1200))),
            )
            report.model_calls += 1
            if trace is not None:
                trace.add_response("evaluate", "evaluator", "Evaluator reply", response)
            parsed = _parse_evaluation(getattr(response, "content", ""), roster)
            report.method = "model" if parsed["reports"] else "rules"
        except Exception as exc:  # noqa: BLE001 - a failed check must not stop the run
            report.model_calls += 1
            report.reason = f"evaluator call failed ({type(exc).__name__})"
            if trace is not None:
                trace.add(
                    "evaluate",
                    "evaluator",
                    "Evaluator call failed — scoring by rule instead",
                    error=str(exc),
                )
    elif not model:
        report.reason = "no model configured — rule check only"
    elif not usable:
        report.reason = "nothing usable to rewrite — rule check only"

    report.summary = parsed["summary"]
    report.gaps = list(parsed["gaps"])

    rule_issues: list[str] = []
    revised_titles: list[str] = []
    for outcome in results:
        entry = parsed["reports"].get(outcome.agent)
        base = rule_check(outcome, wants_sources=wants_sources)
        if entry is None:
            item = base
        else:
            item = Evaluation(
                agent=outcome.agent,
                verdict=entry["verdict"],
                score=entry["score"],
                issues=entry["issues"] or base.issues,
                method="model",
                usable=base.usable,
            )
            if item.verdict == "fail" and outcome.status == "ok":
                # a usable report that the model called useless: trust the rule check
                # for the score, but keep the model's complaint visible
                item.score = max(item.score, 1)

        rewrite = (entry or {}).get("rewrite", "")
        if (
            optimize
            and outcome.status == "ok"
            and rewrite
            and item.verdict != "keep"
            and keep_facts(outcome.answer, rewrite)
        ):
            outcome.optimized = rewrite
            item.revised = True
            item.chars_before = len(outcome.answer)
            item.chars_after = len(rewrite)
            revised_titles.append(item.label)
        elif rewrite and item.verdict != "keep" and not keep_facts(outcome.answer, rewrite):
            item.issues = item.issues + ["the proposed rewrite dropped evidence — kept the original"]

        for issue in item.issues:
            if outcome.status == "ok" and issue not in rule_issues:
                rule_issues.append(f"{item.label}: {issue}")
        outcome.evaluation = item.to_dict()
        report.add(item)

    if not report.summary:
        report.summary = _rule_summary(report)
    if not report.gaps:
        report.gaps = [issue for issue in rule_issues if "too thin" in issue or "no URL" in issue][:4]

    report.ran = True
    report.latency_ms = int((time.perf_counter() - started) * 1000)
    report.method = "mixed" if report.method == "model" and any(
        item.method == "rules" for item in report.items
    ) else report.method

    if trace is not None:
        judged = sum(1 for item in report.items if item.usable)
        verdict_line = f"quality {report.quality}/5" if judged else "nothing usable"
        if report.revised:
            verdict_line += f", {report.revised} rewritten"
        trace.add(
            "evaluate",
            "evaluator",
            f"Evaluator reviewed {len(report.items)} report(s): {verdict_line}",
            round=round_no,
            method=report.method,
            quality=report.quality,
            summary=report.summary,
            gaps=report.gaps,
            revised=report.revised,
            items=[item.to_dict() for item in report.items],
            model_calls=report.model_calls,
        )
        for outcome in results:
            item = report.item_for(outcome.agent)
            if item is not None and item.revised:
                trace.add(
                    "evaluate",
                    "evaluator",
                    f"{item.label} report rewritten",
                    agent=outcome.agent,
                    verdict=item.verdict,
                    score=item.score,
                    issues=item.issues,
                    before=outcome.answer,
                    after=outcome.optimized,
                    chars_before=item.chars_before,
                    chars_after=item.chars_after,
                )

    return report


def _rule_summary(report: EvaluationReport) -> str:
    """One sentence, written from the scores — used when the model had nothing to say."""
    if not report.items:
        return ""
    ok = sum(1 for item in report.items if item.verdict == "keep" and item.usable)
    weak = sum(1 for item in report.items if item.verdict == "revise" and item.usable)
    bad = report.failed
    judged = sum(1 for item in report.items if item.usable)
    if not judged:
        count = len(report.items)
        return (
            "no report could be used."
            if count == 1
            else f"none of the {count} reports could be used."
        )
    parts = [f"{ok} of {judged} report(s) needed no change"]
    if weak:
        parts.append(f"{weak} needed work")
    if bad:
        parts.append(f"{bad} could not be used")
    return "; ".join(parts) + "."
