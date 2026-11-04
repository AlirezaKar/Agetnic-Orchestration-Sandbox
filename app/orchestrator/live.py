"""What the page is allowed to see *while* a run is happening.

Providers do not hand back a model's hidden chain of thought, so there is
nothing to stream in the sense a "reasoning" API means. What this pipeline does
have — and what is genuinely useful to watch — is the **work itself**: the
planner choosing the specialists and writing their briefs, each agent's prompt,
the model's reply, every tool call, the evaluator's verdicts and rewrites, and
the manager's review. That is what this module projects out of a trace event.

Each event becomes one **thought**:

```json
{"seq": 7, "stage": "agent", "actor": "search",
 "category": "agent", "group": "agent:search", "label": "Web researcher",
 "kind": "reply", "title": "Model reply (step 1)",
 "text": "I should search for the latest release first…",
 "offset_ms": 4210, "at": "2026-11-04T09:12:44.120Z", "detail": {"wave": 1}}
```

``group`` is what the page folds the thoughts by — one expandable group per
planner / agent / evaluator / manager — and ``kind`` picks the icon (note,
prompt, reply, tool, verdict, rewrite, status).

Three rules keep this honest and cheap:

* **no system prompts, no hidden state** — a prompt thought carries the *last
  user message* (the task the agent was actually given), never the instructions;
* **no double reveal** — the synthesis reply *is* the answer, so it travels as a
  status thought ("wrote the answer, 812 chars") and the answer arrives once, in
  the bubble;
* **bounded** — every string is clipped here, because this goes over a socket
  that stays open, not into a file.
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.planner import AGENTS

#: how much of one string a live thought may carry by default
THOUGHT_CHARS = 700
#: Errors are the noisiest thing a run produces (TLS blobs, provider HTML), and a
#: thought is a line, not a log — the full text is in the trace and logs/error.log.
_ERROR_CHARS = 240

#: stages that belong to the manager's own thread rather than to an agent
_MANAGER_STAGES = {"run", "merge", "done", "manager"}


def _agent_label(actor: str) -> str:
    return AGENTS.get(actor, {}).get("label", actor.title())


def category_for(event: dict[str, Any]) -> tuple[str, str, str]:
    """``(category, group key, label)`` for one trace event.

    Groups are what the page folds open and shut, so they are keyed by the
    *thing that is thinking*: ``planner``, ``evaluator``, ``manager``, or
    ``agent:<name>`` (tools join the agent that called them).
    """
    stage = str(event.get("stage") or "")
    actor = str(event.get("actor") or "")
    if stage == "planner":
        return "planner", "planner", "Planner"
    if stage == "evaluate":
        return "evaluator", "evaluator", "Evaluator"
    if stage == "synthesis":
        return "manager", "manager", "Manager · writing"
    if stage == "agent" or stage == "tool":
        label = _agent_label(actor)
        return "agent", f"agent:{actor}", label
    if stage in _MANAGER_STAGES:
        return "manager", "manager", "Manager"
    return "manager", "manager", "Manager"


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + " …"


def _last_user_message(detail: dict[str, Any]) -> str:
    """The task an agent was given — the last *user* message of its prompt."""
    messages = detail.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _kind_and_text(
    event: dict[str, Any], detail: dict[str, Any], limit: int
) -> tuple[str, str]:
    """``(kind, text)`` — the one line the page shows next to the icon."""
    stage = str(event.get("stage") or "")
    title = str(event.get("title") or "")
    lowered = title.lower()

    if detail.get("messages") is not None:
        if stage == "synthesis":
            return "prompt", "Writing the answer from the evaluated reports"
        prompt = _last_user_message(detail)
        return ("prompt", _clip(prompt, limit)) if prompt else ("prompt", title)

    if detail.get("content") is not None:
        content = str(detail.get("content") or "").strip()
        if stage == "synthesis":
            return "status", f"Wrote the answer ({len(content)} chars)"
        if not content:
            return "reply", "(the model returned nothing)"
        return "reply", _clip(content, limit)

    if stage == "tool":
        # the trace titles a failure "web_search failed" and carries no name,
        # so the title is the tool — minus the verb, which is added back below
        tool = str(detail.get("tool") or detail.get("name") or title or "tool")
        tool = tool.replace(" failed", "").strip() or "tool"
        query = str(detail.get("query") or "")
        if query:
            return "tool", _clip(f"{tool} · {query}", limit)
        failed = str(detail.get("error") or "")
        if failed:
            return "tool", _clip(f"{tool} failed: {failed}", _ERROR_CHARS)
        return "tool", tool

    if stage == "evaluate":
        items = detail.get("items") or []
        if items:
            scored = ", ".join(
                f"{item.get('label') or item.get('agent')} {item.get('score')}/5"
                for item in items
                if isinstance(item, dict)
            )
            summary = str(detail.get("summary") or "")
            text = f"{summary} — {scored}" if summary else scored
            if detail.get("gaps"):
                text += " · gaps: " + ", ".join(str(gap) for gap in detail["gaps"][:3])
            return "verdict", _clip(text, limit)
        if detail.get("before") or detail.get("chars_before") is not None:
            issues = detail.get("issues") or []
            why = "; ".join(str(issue) for issue in issues[:3]) or "tightened"
            return "rewrite", _clip(
                f"rewrote {detail.get('agent', '')}: {why} "
                f"({detail.get('chars_before', 0)} → {detail.get('chars_after', 0)} chars)",
                limit,
            )
        return "note", _clip(title, limit)

    if stage == "agent":
        bits = [title]
        task = str(detail.get("task") or "")
        if task and "task" in lowered:
            bits = [task]
        if detail.get("error"):
            bits.append(_clip(detail["error"], _ERROR_CHARS))
        return "note", _clip(" — ".join(bit for bit in bits if bit), limit)

    if stage in ("run", "merge", "done"):
        agents = detail.get("agents")
        text = title
        if isinstance(agents, list) and agents:
            text += " · " + ", ".join(str(agent) for agent in agents)
        if detail.get("reason"):
            text += " · " + str(detail["reason"])
        if detail.get("error"):
            text += " · " + _clip(detail["error"], _ERROR_CHARS)
        return "status", _clip(text, limit)

    return "note", _clip(title or event.get("title") or "", limit)


def _detail_out(
    event: dict[str, Any], detail: dict[str, Any], limit: int
) -> dict[str, Any]:
    """The handful of structured fields the page shows as chips under a thought."""
    keep = (
        "task",
        "reason",
        "note",
        "error",
        "status",
        "latency_ms",
        "wave",
        "model",
        "provider",
        "tool",
        "query",
        "revised",
        "quality",
        "round",
        "synthesized",
    )
    out: dict[str, Any] = {}
    for key in keep:
        value = detail.get(key)
        if value is None or value == "" or value == []:
            continue
        out[key] = _clip(value, limit) if isinstance(value, str) else value
    if isinstance(detail.get("agents"), list):
        out["agents"] = [str(agent) for agent in detail["agents"]][:8]
    if isinstance(detail.get("items"), list):
        out["items"] = [
            {
                "agent": item.get("agent"),
                "label": item.get("label"),
                "verdict": item.get("verdict"),
                "score": item.get("score"),
                "revised": bool(item.get("revised")),
            }
            for item in detail["items"]
            if isinstance(item, dict)
        ]
    if detail.get("gaps"):
        out["gaps"] = [str(gap) for gap in detail["gaps"]][:4]
    if detail.get("tool_calls"):
        out["tools"] = [
            str(call.get("name") or "")
            for call in detail["tool_calls"]
            if isinstance(call, dict)
        ][:6]
    return out


def thought(event: dict[str, Any], *, limit: int = THOUGHT_CHARS) -> dict[str, Any]:
    """One trace event as the page sees it while the run is in flight."""
    detail = event.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    category, group, label = category_for(event)
    kind, text = _kind_and_text(event, detail, limit)
    return {
        "seq": event.get("seq"),
        "stage": event.get("stage"),
        "actor": event.get("actor"),
        "category": category,
        "group": group,
        "label": label,
        "kind": kind,
        "title": str(event.get("title") or ""),
        "text": text,
        "offset_ms": event.get("offset_ms", 0),
        "at": event.get("at", ""),
        "detail": _detail_out(event, detail, limit),
    }
