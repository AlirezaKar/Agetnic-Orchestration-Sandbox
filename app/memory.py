"""Harness memory: the conversation itself, kept on disk and resumable.

The run log in ``logs/orchestrator/`` answers *"what did the machine do?"* —
it is a forensic record. Memory answers a different question: *"what did we
say to each other?"*. It stores the user turns and the assistant turns exactly
as the browser rendered them, so a session can be reopened later, shown with
the same bubbles and the same "How this answer was built" panel, and continued
with the earlier conversation as context.

```
memory/
  index.jsonl                 one line per session change (newest last)
  sessions/<session-id>.json  the whole conversation: every turn, both sides
  sessions/<session-id>.md    the same conversation as readable markdown
```

Why JSON and not a database: a conversation is a small, self-contained
document that benefits from being greppable, diffable, backup-able with ``cp``
and readable without the app. SQLite would only pay off past thousands of
sessions, and it would hide the memory from every tool that already works on
files. The layout mirrors the run log on purpose: index for listing, one file
per item for the detail.

Nothing here is a log: memory survives restarts, memory is what makes the next
turn feel like a continuation. Writes never break a chat — a read-only
checkout degrades to an in-process store and says so in ``/health``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Iterable

from app import logs

INDEX_NAME = "index.jsonl"
SESSIONS_DIR = "sessions"
DEFAULT_MAX_TURNS = 200
DEFAULT_HISTORY_TURNS = 6
DEFAULT_HISTORY_CHARS = 3000
_MODES = ("auto", "agent", "answer", "search")


# --------------------------------------------------------------------- helpers
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if limit and len(value) > limit:
        return value[:limit] + f"… (+{len(value) - limit} chars)"
    return value


def safe_id(value: str | None) -> str | None:
    """Session ids are used as file names: only ``[A-Za-z0-9-_]`` survive."""
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    return cleaned if cleaned and cleaned == value else None


def new_session_id() -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"s-{stamp}-{secrets.token_hex(3)}"


def title_from(text: str, limit: int = 72) -> str:
    """A readable title from the first thing the user said."""
    flat = re.sub(r"\s+", " ", (text or "").strip())
    if not flat:
        return "Untitled session"
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def format_search_answer(data: dict[str, Any]) -> str:
    """The text the page shows for a search-only turn (mirrored in app.js)."""
    lines = [
        f"{index + 1}. {item.get('title', '')}\n   {item.get('url', '')}\n   {item.get('snippet', '')}"
        for index, item in enumerate(data.get("results") or [])
    ]
    head = (
        f"Search backend: {data.get('provider', '?')} · {data.get('count', 0)} result(s)"
        f" · {data.get('latency_ms', 0)} ms"
    )
    return head + "\n\n" + ("\n\n".join(lines) if lines else "No results.")


def turn_text(turn: dict[str, Any]) -> str:
    """The assistant text of a turn, as it was displayed."""
    assistant = turn.get("assistant") or {}
    return (
        assistant.get("answer")
        or assistant.get("text")
        or (assistant.get("response") or {}).get("answer")
        or ""
    )


def turn_files(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return (turn.get("user") or {}).get("files") or []


# ----------------------------------------------------------------- the store
class SessionMemory:
    """Conversation store: create, append, read, rename, forget, export."""

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        enabled: bool = True,
        max_turns: int = DEFAULT_MAX_TURNS,
        history_turns: int = DEFAULT_HISTORY_TURNS,
        history_chars: int = DEFAULT_HISTORY_CHARS,
    ) -> None:
        self.enabled = bool(enabled)
        self.dir = Path(directory) if directory else Path(
            os.environ.get("MEMORY_DIR", "").strip() or "memory"
        )
        self.sessions_dir = self.dir / SESSIONS_DIR
        self.max_turns = max(int(max_turns or 0), 1)
        self.history_turns = max(int(history_turns or 0), 0)
        self.history_chars = max(int(history_chars or 0), 200)
        self._memory: dict[str, dict[str, Any]] = {}  # fallback when disk fails
        self.writable = True
        if self.enabled:
            try:
                self.sessions_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - read-only checkouts
                self.writable = False
                logs.get_logger("memory").warning(
                    "memory directory %s is not writable (%s): sessions are in-process only",
                    self.dir,
                    exc,
                )

    # ------------------------------------------------------------- internals
    def _path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def _md_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.md"

    def _write_json(self, record: dict[str, Any]) -> Path | None:
        target = self._path(record["session_id"])
        try:
            # the directory may have been removed while the app was running
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
            self._md_path(record["session_id"]).write_text(
                render_markdown(record), encoding="utf-8"
            )
            return target
        except OSError as exc:  # pragma: no cover - never break a chat
            self.writable = False
            logs.get_logger("memory").warning("could not write session %s: %s", record["session_id"], exc)
            return None

    def _next_seq(self) -> int:
        """Write order, persisted: newest first even when two turns share a timestamp."""
        return max(
            (int(item.get("seq", 0) or 0) for item in self._load_index().values()), default=0
        ) + 1

    def _index_line(self, record: dict[str, Any], *, deleted: bool = False) -> None:
        line = summary_of(record)
        line["seq"] = self._next_seq()
        line["deleted"] = deleted
        try:
            path = self.dir / INDEX_NAME
            if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
                path.replace(self.dir / "index.1.jsonl")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover
            pass

    def _load_index(self) -> dict[str, dict[str, Any]]:
        """Last state wins: the index is append-only, deletions are tombstones."""
        path = self.dir / INDEX_NAME
        entries: dict[str, dict[str, Any]] = {}
        if not path.exists():
            return entries
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:  # pragma: no cover
            return entries
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            session_id = item.get("session_id")
            if not session_id:
                continue
            if item.get("deleted"):
                entries.pop(session_id, None)
            else:
                entries[session_id] = item
        return entries

    def _remember(self, record: dict[str, Any]) -> None:
        self._memory[record["session_id"]] = record

    # ---------------------------------------------------------------- public
    def create(self, *, title: str = "", mode: str = "auto") -> dict[str, Any]:
        """A new, empty session (no turn yet) — used by ``POST /sessions``."""
        record = {
            "version": 1,
            "session_id": new_session_id(),
            "title": title.strip() or "New session",
            "created_at": _now(),
            "updated_at": _now(),
            "modes": [mode] if mode in _MODES else [],
            "turns": [],
            "stats": {"turns": 0, "model_calls": 0, "agents": {}, "latency_ms": 0},
        }
        self._remember(record)
        self._write_json(record)
        self._index_line(record)
        return record

    def get(self, session_id: str | None) -> dict[str, Any] | None:
        """The full session record, from memory, disk, or nothing."""
        safe = safe_id(session_id)
        if not safe:
            return None
        if safe in self._memory:
            return self._memory[safe]
        path = self._path(safe)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - corrupt file
            return None
        if not isinstance(record, dict) or not record.get("session_id"):
            return None
        record.setdefault("turns", [])
        record.setdefault("stats", {})
        self._remember(record)
        return record

    def exists(self, session_id: str | None) -> bool:
        return self.get(session_id) is not None

    def append(
        self,
        session_id: str | None,
        *,
        mode: str = "auto",
        user_text: str = "",
        user_files: Iterable[dict[str, Any]] | None = None,
        response: dict[str, Any] | None = None,
        ok: bool = True,
        error: dict[str, Any] | None = None,
        run_id: str = "",
        provider: str = "",
        model: str = "",
        latency_ms: int = 0,
    ) -> dict[str, Any] | None:
        """Store one exchange (user message + assistant answer) and return its info.

        ``response`` is the payload the browser received, so replaying a session
        renders exactly what was rendered the first time.
        Returns ``None`` when memory is disabled.
        """
        if not self.enabled:
            return None
        record = self.get(session_id)
        if record is None:
            record = self.create(title=title_from(user_text), mode=mode)
        if not record.get("title") or record["title"] == "New session":
            record["title"] = title_from(user_text)

        response = response or {}
        assistant: dict[str, Any] = {
            "ok": bool(ok),
            "answer": response.get("answer") or "",
            "provider": response.get("provider") or provider,
            "model": response.get("model") or model,
            "latency_ms": int(response.get("latency_ms") or latency_ms or 0),
            "run_id": response.get("run_id") or run_id,
            "response": response,
            "error": error,
        }
        if mode == "search" and not assistant["answer"]:
            assistant["text"] = format_search_answer(response)
        orchestration = response.get("orchestration") or {}
        if orchestration:
            assistant["agents"] = (orchestration.get("plan") or {}).get("agents", [])
            assistant["model_calls"] = orchestration.get("model_calls", 0)
            assistant["trace_steps"] = len(orchestration.get("trace") or [])

        turn = {
            "turn_id": f"t-{len(record['turns']) + 1}",
            "at": _now(),
            "mode": mode if mode in _MODES else "auto",
            "user": {"text": user_text, "files": list(user_files or [])},
            "assistant": assistant,
        }
        record["turns"].append(turn)
        if len(record["turns"]) > self.max_turns:
            record["turns"] = record["turns"][-self.max_turns :]
        if turn["mode"] not in record.setdefault("modes", []):
            record["modes"].append(turn["mode"])
        record["updated_at"] = turn["at"]
        self._bump_stats(record, turn)
        self._remember(record)
        path = self._write_json(record)
        self._index_line(record)
        return {
            "session_id": record["session_id"],
            "turn_id": turn["turn_id"],
            "index": len(record["turns"]),
            "title": record["title"],
            "turns": len(record["turns"]),
            "created_at": record["created_at"],
            "at": turn["at"],
            "path": str(path) if path else "",
            "stored": path is not None,
        }

    def _bump_stats(self, record: dict[str, Any], turn: dict[str, Any]) -> None:
        stats = record.setdefault("stats", {})
        stats["turns"] = len(record["turns"])
        assistant = turn["assistant"]
        stats["model_calls"] = int(stats.get("model_calls", 0) or 0) + int(
            assistant.get("model_calls", 0) or 0
        )
        stats["latency_ms"] = int(stats.get("latency_ms", 0) or 0) + int(
            assistant.get("latency_ms", 0) or 0
        )
        agents = stats.setdefault("agents", {})
        for name in assistant.get("agents") or []:
            agents[name] = int(agents.get(name, 0) or 0) + 1

    def history(
        self,
        session_id: str | None,
        *,
        turns: int | None = None,
        chars: int | None = None,
    ) -> list[dict[str, str]]:
        """The last turns as ``[{role, content}]`` — the model's context."""
        record = self.get(session_id)
        if not record:
            return []
        limit = self.history_turns if turns is None else max(int(turns), 0)
        size = self.history_chars if chars is None else max(int(chars), 200)
        if limit <= 0:
            return []
        out: list[dict[str, str]] = []
        for turn in (record.get("turns") or [])[-limit:]:
            text = (turn.get("user") or {}).get("text") or ""
            if text:
                out.append({"role": "user", "content": _clip(text, size)})
            if (turn.get("assistant") or {}).get("ok"):
                answer = turn_text(turn)
                if answer:
                    out.append({"role": "assistant", "content": _clip(answer, size)})
        return out

    def rename(self, session_id: str | None, title: str) -> dict[str, Any] | None:
        record = self.get(session_id)
        if record is None:
            return None
        record["title"] = title.strip()[:120] or record.get("title") or "Untitled session"
        record["updated_at"] = _now()
        self._remember(record)
        self._write_json(record)
        self._index_line(record)
        return record

    def delete(self, session_id: str | None) -> bool:
        """Forget a session for good (file, markdown copy, index tombstone)."""
        safe = safe_id(session_id)
        if not safe:
            return False
        record = self.get(safe)
        if record is None:
            return False
        self._memory.pop(safe, None)
        for path in (self._path(safe), self._md_path(safe)):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
        self._index_line(record, deleted=True)
        return True

    def clear(self, session_id: str | None) -> dict[str, Any] | None:
        """Empty a session but keep its id (and its title)."""
        record = self.get(session_id)
        if record is None:
            return None
        record["turns"] = []
        record["stats"] = {"turns": 0, "model_calls": 0, "agents": {}, "latency_ms": 0}
        record["updated_at"] = _now()
        self._remember(record)
        self._write_json(record)
        self._index_line(record)
        return record

    def summary(self, session_id: str | None) -> dict[str, Any] | None:
        record = self.get(session_id)
        return summary_of(record) if record else None

    def list(self, limit: int = 50, query: str = "") -> list[dict[str, Any]]:
        """Recent sessions, newest first, without loading the conversations."""
        entries = list(self._load_index().values())
        needle = (query or "").strip().lower()
        if needle:
            entries = [
                item
                for item in entries
                if needle in (item.get("title") or "").lower()
                or needle in (item.get("last_user") or "").lower()
                or needle in (item.get("last_answer") or "").lower()
            ]
        entries.sort(
            key=lambda item: (int(item.get("seq", 0) or 0), item.get("updated_at") or ""),
            reverse=True,
        )
        return entries[: max(int(limit), 1)]

    def stats(self) -> dict[str, Any]:
        entries = list(self._load_index().values())
        return {
            "enabled": self.enabled,
            "dir": str(self.dir),
            "writable": bool(self.writable),
            "sessions": len(entries),
            "turns": sum(int(item.get("turns", 0) or 0) for item in entries),
            "history_turns": self.history_turns,
            "max_turns": self.max_turns,
        }

    def export_markdown(self, session_id: str | None) -> str | None:
        record = self.get(session_id)
        return render_markdown(record) if record else None

    def touch(self, session_id: str) -> None:
        """Record a visit so the list stays useful without changing content."""
        record = self.get(session_id)
        if record is None:
            return
        record["updated_at"] = _now()
        self._remember(record)
        self._write_json(record)
        self._index_line(record)

    def forget_all(self) -> int:
        """Delete every session (``python -m scripts.memory forget --all``)."""
        removed = 0
        for session_id in list(self._load_index()):
            if self.delete(session_id):
                removed += 1
        return removed


# ------------------------------------------------------------ rendering bits
def summary_of(record: dict[str, Any]) -> dict[str, Any]:
    """The small view of a session that a list needs."""
    turns = record.get("turns") or []
    last = turns[-1] if turns else {}
    response = (last.get("assistant") or {}).get("response") or {}
    return {
        "session_id": record.get("session_id", ""),
        "title": record.get("title", "Untitled session"),
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
        "turns": len(turns),
        "modes": record.get("modes", []),
        "last_user": ((last.get("user") or {}).get("text") or "")[:200],
        "last_answer": (
            turn_text(last)
            or ((last.get("assistant") or {}).get("error") or {}).get("body", {}).get("message", "")
        )[:200],
        "run_id": response.get("run_id", ""),
        "agents": ((response.get("orchestration") or {}).get("plan") or {}).get("agents", []),
        "model": (last.get("assistant") or {}).get("model", ""),
        "provider": (last.get("assistant") or {}).get("provider", ""),
        "stats": record.get("stats", {}),
    }


def render_markdown(record: dict[str, Any]) -> str:
    """A human-readable transcript — the memory you can read in any editor."""
    lines = [
        f"# {record.get('title', 'Untitled session')}",
        "",
        f"- session: `{record.get('session_id', '')}`",
        f"- created: {record.get('created_at', '')}",
        f"- updated: {record.get('updated_at', '')}",
        f"- turns: {len(record.get('turns') or [])}",
        "",
    ]
    for index, turn in enumerate(record.get("turns") or [], start=1):
        assistant = turn.get("assistant") or {}
        user = turn.get("user") or {}
        meta = [turn.get("mode", ""), turn.get("at", "")]
        if assistant.get("agents"):
            meta.append("agents: " + " → ".join(assistant["agents"]))
        if assistant.get("model"):
            meta.append(f"{assistant.get('provider', '')}/{assistant['model']}")
        if assistant.get("latency_ms"):
            meta.append(f"{assistant['latency_ms']} ms")
        lines += ["---", "", f"## {index} · " + " · ".join(m for m in meta if m), ""]
        if user.get("files"):
            names = ", ".join(item.get("name", "file") for item in user["files"])
            lines += [f"_attached: {names}_", ""]
        lines += ["**You**", "", user.get("text", "").strip() or "(empty)", "", "**Assistant**", ""]
        if assistant.get("ok"):
            lines += [turn_text(turn).strip() or "(empty answer)", ""]
        else:
            error = (assistant.get("error") or {})
            body = error.get("body") or {}
            lines += [f"> **failed** — {body.get('message') or error.get('message') or 'unknown error'}", ""]
        if assistant.get("run_id"):
            lines += [f"<sub>run `{assistant['run_id']}` · trace steps: {assistant.get('trace_steps', 0)}</sub>", ""]
    return "\n".join(lines).rstrip() + "\n"


def session_view(record: dict[str, Any], *, history_turns: int = DEFAULT_HISTORY_TURNS) -> dict[str, Any]:
    """What ``GET /sessions/{id}`` returns: the conversation plus a resume hint."""
    view = dict(record)
    view["summary"] = summary_of(record)
    view["resume"] = {
        "session_id": record.get("session_id", ""),
        "turns": len(record.get("turns") or []),
        "history_turns": history_turns,
        "hint": "POST /orchestrate (or /chat) with this session_id to continue the conversation.",
    }
    return view
