#!/usr/bin/env python3
"""Read, search and prune the harness memory — the conversations themselves.

    python -m scripts.memory list                  # recent sessions
    python -m scripts.memory list -q refund        # filter title / last message / last answer
    python -m scripts.memory show <session-id>     # every turn, both sides
    python -m scripts.memory show <id> --full      # no clipping
    python -m scripts.memory search refunds        # grep the conversations on disk
    python -m scripts.memory export <id>           # print the markdown transcript
    python -m scripts.memory export <id> -o chat.md
    python -m scripts.memory forget <id>           # delete one session
    python -m scripts.memory forget --all          # delete every session
    python -m scripts.memory path                  # where memory lives

Memory is not a log: it is what makes the next turn a continuation of the last
one. Runtime logs stay in ``logs/`` (``python -m scripts.logs``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.memory import SessionMemory, turn_files, turn_text


def _store(args) -> SessionMemory:
    return SessionMemory(args.memory_dir or None)


def _when(value: str) -> str:
    return (value or "").replace("T", " ")[:19]


def cmd_path(args) -> int:
    store = _store(args)
    print(store.dir)
    return 0


def cmd_list(args) -> int:
    store = _store(args)
    items = store.list(args.limit, args.q or "")
    if not items:
        print(f"no conversations in {store.dir} yet — send a message and they appear here")
        return 1
    width = min(max((len(item["session_id"]) for item in items), default=12), 30)
    print(f"{'session':<{width}}  {'turns':>5}  {'updated':<19}  title")
    for item in items:
        print(
            f"{item['session_id']:<{width}}  {item['turns']:>5}  "
            f"{_when(item.get('updated_at', '')):<19}  {(item.get('title') or '')[:60]}"
        )
    stats = store.stats()
    print(
        f"\n{stats['sessions']} session(s) · {stats['turns']} turn(s) · {stats['dir']}"
        "   (python -m scripts.memory show <id>)"
    )
    return 0


def cmd_show(args) -> int:
    store = _store(args)
    record = store.get(args.session_id)
    if record is None:
        print(f"error: no session '{args.session_id}' in {store.dir}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    limit = 0 if args.full else args.chars
    print(f"# {record.get('title', '')}")
    print(f"session   {record.get('session_id')}  ·  created {_when(record.get('created_at', ''))}")
    print(f"updated   {_when(record.get('updated_at', ''))}  ·  {len(record.get('turns') or [])} turn(s)")
    stats = record.get("stats") or {}
    if stats:
        agents = ", ".join(f"{name}×{count}" for name, count in (stats.get("agents") or {}).items())
        print(f"stats     {stats.get('model_calls', 0)} model call(s) · {agents or 'no agents'}")
    for index, turn in enumerate(record.get("turns") or [], start=1):
        assistant = turn.get("assistant") or {}
        head = [f"[{index}] {turn.get('mode', '?')}", _when(turn.get("at", ""))]
        if assistant.get("agents"):
            head.append("agents: " + " → ".join(assistant["agents"]))
        if assistant.get("model"):
            head.append(f"{assistant.get('provider', '')}/{assistant['model']}")
        if assistant.get("latency_ms"):
            head.append(f"{assistant['latency_ms']} ms")
        files = [item.get("name", "file") for item in turn_files(turn)]
        print("\n" + " · ".join(head) + (f"  · files: {', '.join(files)}" if files else ""))
        print("  you │ " + ((turn.get("user") or {}).get("text") or "").replace("\n", "\n      │ "))
        if assistant.get("ok") is False:
            error = (assistant.get("error") or {}).get("body") or {}
            print("   ai │ FAILED — " + str(error.get("message") or "unknown error"))
            continue
        text = turn_text(turn)
        if limit and len(text) > limit:
            text = text[:limit] + f"… (+{len(text) - limit} chars, --full for all)"
        print("   ai │ " + (text or "(empty)").replace("\n", "\n      │ "))
        if assistant.get("run_id"):
            print(
                f"      └ run {assistant['run_id']} · "
                f"{assistant.get('trace_steps', 0)} trace step(s)"
            )
    print(f"\nfile: {store.dir / 'sessions' / (record.get('session_id', '') + '.json')}")
    return 0


def cmd_search(args) -> int:
    store = _store(args)
    needle = " ".join(args.text).strip()
    if not needle:
        print("error: give something to search for", file=sys.stderr)
        return 1
    hits = 0
    for item in store.list(500):
        record = store.get(item["session_id"])
        if not record:
            continue
        for index, turn in enumerate(record.get("turns") or [], start=1):
            blob = "\n".join(
                [
                    (turn.get("user") or {}).get("text") or "",
                    turn_text(turn),
                    (turn.get("assistant") or {}).get("answer") or "",
                ]
            )
            if needle.lower() not in blob.lower():
                continue
            hits += 1
            snippet = ""
            lowered = blob.lower()
            start = max(lowered.find(needle.lower()) - 60, 0)
            snippet = blob[start : start + 160].replace("\n", " ")
            print(f"{record['session_id']}  turn {index}  {(record.get('title') or '')[:50]}")
            print(f"    … {snippet.strip()} …")
    if not hits:
        print(f"no conversation in {store.dir} mentions {needle!r}")
        return 1
    print(f"\n{hits} turn(s) matched")
    return 0


def cmd_export(args) -> int:
    store = _store(args)
    markdown = store.export_markdown(args.session_id)
    if markdown is None:
        print(f"error: no session '{args.session_id}' in {store.dir}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"wrote {args.out} ({len(markdown)} chars)")
    else:
        print(markdown)
    return 0


def cmd_forget(args) -> int:
    store = _store(args)
    if args.all:
        removed = store.forget_all()
        print(f"removed {removed} session(s) from {store.dir}")
        return 0
    if not args.session_id:
        print("error: give a session id, or --all", file=sys.stderr)
        return 1
    if not store.delete(args.session_id):
        print(f"error: no session '{args.session_id}' in {store.dir}", file=sys.stderr)
        return 1
    print(f"forgot {args.session_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.memory",
        description="Inspect the harness memory (conversations), not the program logs.",
    )
    parser.add_argument("--memory-dir", default=None, help="memory directory (default: MEMORY_DIR or memory/)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("path", help="print the memory directory").set_defaults(func=cmd_path)

    listing = sub.add_parser("list", help="recent conversations")
    listing.add_argument("-l", "--limit", type=int, default=20)
    listing.add_argument("-q", "--q", default="", help="filter on title, last message or last answer")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="every turn of one conversation")
    show.add_argument("session_id")
    show.add_argument("--chars", type=int, default=1200, help="clip each answer (0 = no clip)")
    show.add_argument("--full", action="store_true", help="print every answer in full")
    show.add_argument("--json", action="store_true", help="dump the stored record as JSON")
    show.set_defaults(func=cmd_show)

    searching = sub.add_parser("search", help="grep every conversation")
    searching.add_argument("text", nargs="+")
    searching.set_defaults(func=cmd_search)

    export = sub.add_parser("export", help="markdown transcript of one conversation")
    export.add_argument("session_id")
    export.add_argument("-o", "--out", default=None, help="write to a file instead of stdout")
    export.set_defaults(func=cmd_export)

    forget = sub.add_parser("forget", help="delete one conversation (or --all)")
    forget.add_argument("session_id", nargs="?")
    forget.add_argument("--all", action="store_true", help="delete every conversation")
    forget.set_defaults(func=cmd_forget)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
