#!/usr/bin/env python3
"""Read the program logs without remembering paths or fighting a pager.

    python -m scripts.logs list                 # every file, size, last write
    python -m scripts.logs tail                 # last 50 lines of agetnic.log
    python -m scripts.logs tail error -n 200    # last 200 lines of error.log
    python -m scripts.logs tail -f              # follow agetnic.log (Ctrl-C to stop)
    python -m scripts.logs errors               # full records: message + traceback
    python -m scripts.logs requests             # HTTP lines, slowest/failing last
    python -m scripts.logs requests --status 4xx
    python -m scripts.logs process              # last starts: pid, port, cwd, git
    python -m scripts.logs path                 # print the log directory

Exit codes: 0 ok, 1 nothing found / log dir missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from app import logs


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}G"


def cmd_path(args) -> int:
    print(logs.log_dir(args.log_dir))
    return 0


def cmd_list(args) -> int:
    entries = logs.list_logs(args.log_dir)
    if not entries:
        print(f"no logs yet in {logs.log_dir(args.log_dir)} — run `python run.py` first")
        return 1
    width = max(len(entry["name"]) for entry in entries)
    print(f"{'file':<{width}}  {'size':>8}  last write")
    for entry in entries:
        print(
            f"{entry['name']:<{width}}  {_human(entry['bytes']):>8}  "
            f"{entry['modified']:%Y-%m-%d %H:%M:%S}"
        )
    return 0


def cmd_tail(args) -> int:
    try:
        path = logs.resolve_log_file(args.file, args.log_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"error: {path} does not exist yet (start the server first)", file=sys.stderr)
        return 1

    for line in logs.tail(path, args.lines):
        print(line)
    if not args.follow:
        return 0
    print(f"--- following {path} (Ctrl-C to stop) ---", file=sys.stderr)
    try:
        for line in logs.follow(path):
            print(line)
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_errors(args) -> int:
    path = logs.resolve_log_file("error", args.log_dir)
    records = logs.read_errors(path, count=args.count, limit=args.limit)
    if not records:
        print(f"no warnings or errors recorded in {path}")
        return 1
    for record in records:
        print(record)
        print("-" * 72)
    return 0


_REQUEST = re.compile(
    r"\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (?P<path>\S+)(?:\?\S*)? -> (?P<status>\d{3}) "
    r"(?P<ms>[0-9.]+)ms req=(?P<req>\S+) client=(?P<client>\S+) bytes=(?P<bytes>\d+)"
)


def cmd_requests(args) -> int:
    path = logs.resolve_log_file("requests", args.log_dir)
    lines = logs.tail(path, args.count)
    parsed = [match for line in lines if (match := _REQUEST.search(line))]
    if not parsed:
        print(f"no HTTP requests recorded in {path}")
        return 1

    selected = parsed
    if args.status:
        prefix = args.status.rstrip("x")
        selected = [m for m in parsed if m.group("status").startswith(prefix)]
    if args.slowest:
        selected = sorted(selected, key=lambda m: float(m.group("ms")), reverse=True)[: args.slowest]

    by_status: dict[str, int] = {}
    for match in parsed:
        key = match.group("status")[0] + "xx"
        by_status[key] = by_status.get(key, 0) + 1
    total_ms = sum(float(m.group("ms")) for m in parsed)
    summary = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
    print(f"{len(parsed)} requests ({summary}) — {total_ms / len(parsed):.1f}ms average")
    print("-" * 72)
    for match in selected[-args.limit :]:
        print(
            f"{match.group('status')}  {match.group('ms'):>8}ms  "
            f"{match.group('method'):<6} {match.group('path')}  req={match.group('req')}"
        )
    return 0


def cmd_process(args) -> int:
    path = logs.resolve_log_file("process", args.log_dir)
    lines = [line for line in logs.tail(path, args.count) if line.strip().startswith("{")]
    if not lines:
        print(f"no process records in {path}")
        return 1
    for line in lines[-args.limit :]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(line)
            continue
        print(
            f"{record.get('timestamp', '?')}  pid={record.get('pid')}  "
            f"port={record.get('port', '-')}  version={record.get('version')}  "
            f"git={record.get('git')}  start={record.get('start', '-')}"
        )
        print(f"    cwd={record.get('cwd')}  python={record.get('python')}  argv={' '.join(record.get('argv') or [])}")
        if record.get("problems"):
            print(f"    problems={record['problems']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.logs",
        description="Inspect the program logs in logs/ (or $LOG_DIR).",
    )
    parser.add_argument("--log-dir", default=None, help="log directory (default: logs/ or LOG_DIR)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("path", help="print the log directory").set_defaults(func=cmd_path)
    sub.add_parser("list", help="files, sizes, last write").set_defaults(func=cmd_list)

    tail = sub.add_parser("tail", help="last lines of a log file")
    tail.add_argument("file", nargs="?", default="agetnic", help="agetnic | error | requests | terminal | process")
    tail.add_argument("-n", "--lines", type=int, default=50)
    tail.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    tail.set_defaults(func=cmd_tail)

    errors = sub.add_parser("errors", help="warning/error records with tracebacks")
    errors.add_argument("-c", "--count", type=int, default=0, help="only the last N records")
    errors.add_argument("-l", "--limit", type=int, default=10)
    errors.set_defaults(func=cmd_errors)

    requests = sub.add_parser("requests", help="HTTP requests, with a status summary")
    requests.add_argument("-n", "--count", type=int, default=500, help="how many lines to read")
    requests.add_argument("-l", "--limit", type=int, default=20, help="how many to print")
    requests.add_argument("--status", default=None, help="filter: 2xx | 4xx | 5xx")
    requests.add_argument("--slowest", type=int, default=0, help="print the N slowest instead")
    requests.set_defaults(func=cmd_requests)

    process = sub.add_parser("process", help="startup records (pid, port, cwd, git)")
    process.add_argument("-c", "--count", type=int, default=200)
    process.add_argument("-l", "--limit", type=int, default=5)
    process.set_defaults(func=cmd_process)
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
