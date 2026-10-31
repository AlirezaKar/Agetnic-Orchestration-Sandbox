#!/usr/bin/env python3
"""One command: check the configuration, then start the server.

    python -m scripts.start                 # checks, then http://localhost:8000
    python -m scripts.start --check-only    # only the checks (CI, first setup)
    python -m scripts.start --port 9000 --reload
    python -m scripts.start --agent         # include a full model+search round trip
    python -m scripts.start --force         # serve even if the checks fail

The three steps are the ones from the README quickstart, in order:

    1. scripts.check_providers   what is configured, what is missing, where to get it
    2. scripts.doctor            does the model answer, does search answer
    3. run.py                    uvicorn serving UI + API on one port

Exit codes: 0 served and stopped normally, 1 checks failed (nothing served),
the server's own code when it exits.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Step:
    name: str
    argv: list[str]
    label: str


def build_steps(args: argparse.Namespace) -> list[Step]:
    """The check commands, in order. Pure so it can be tested without running them."""
    providers = Step(
        name="providers",
        label="provider table (keys + models + links)",
        argv=[sys.executable, "-m", "scripts.check_providers"],
    )
    doctor_argv = [sys.executable, "-m", "scripts.doctor"]
    if args.agent:
        doctor_argv.append("--agent")
    if args.query:
        doctor_argv.extend(["-q", args.query])
    doctor = Step(name="doctor", label="doctor (model ping + search ping)", argv=doctor_argv)
    return [providers, doctor]


def serve_command(args: argparse.Namespace) -> list[str]:
    """The final step: the server, via run.py so the banner and logs are used."""
    argv = [sys.executable, "run.py"]
    if args.port is not None:
        argv.extend(["--port", str(args.port)])
    if args.host:
        argv.extend(["--host", args.host])
    if args.reload:
        argv.append("--reload")
    if args.log_level:
        argv.extend(["--log-level", args.log_level])
    return argv


def decide(
    failures: list[str], *, force: bool, interactive: bool, check_only: bool
) -> str:
    """What to do after the checks: ``serve``, ``stop`` or ``ask``."""
    if check_only:
        return "stop"
    if not failures:
        return "serve"
    if force:
        return "serve"
    return "ask" if interactive else "stop"


def say(text: str = "") -> None:
    """print(), but flushed: the child processes write straight to the terminal."""
    print(text, flush=True)


def _run(step: Step, timeout: float) -> int:
    say(f"\n== {step.label}")
    say(f"   $ {' '.join(step.argv)}")
    say("-" * 72)
    try:
        completed = subprocess.run(step.argv, cwd=ROOT, timeout=timeout)
        return completed.returncode
    except subprocess.TimeoutExpired:
        print(f"\n  ! {step.name} timed out after {timeout:.0f}s (network?)")
        return 124
    except FileNotFoundError as exc:  # pragma: no cover - python always exists
        say(f"\n  ! could not run {step.name}: {exc}")
        return 127


def _ask(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.start",
        description="Check the setup, then start the UI + API.",
    )
    parser.add_argument("--host", default=None, help="bind address (default from .env)")
    parser.add_argument("--port", type=int, default=None, help="port (default: PORT in .env, else 8000)")
    parser.add_argument("--reload", action="store_true", help="auto-reload the server")
    parser.add_argument("--agent", action="store_true", help="also run the agent round trip")
    parser.add_argument("-q", "--query", default=None, help="search query for the doctor check")
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--check-only", action="store_true", help="run the checks, do not serve")
    parser.add_argument("--no-checks", action="store_true", help="skip straight to the server")
    parser.add_argument("--force", action="store_true", help="serve even if a check failed")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per check")
    args = parser.parse_args(argv)

    say("=" * 72)
    say("  Agetnic Orchestration Sandbox — start")
    say("=" * 72)
    if not (ROOT / ".env").exists():
        say("  note: no .env yet — cp .env.example .env, fill one key + its model")

    failures: list[str] = []
    if not args.no_checks:
        for step in build_steps(args):
            if _run(step, args.timeout) != 0:
                failures.append(step.name)
    else:
        say("\n(skipping checks: --no-checks)")

    say("\n" + "=" * 72)
    if failures:
        say(f"  checks failed: {', '.join(failures)}")
        say("  fix .env (each provider needs a key AND a model), then run this again")
        say("  logs: logs/error.log · python -m scripts.logs errors")
    elif args.no_checks:
        say("  checks skipped (--no-checks)")
    else:
        say("  checks passed")
    say("=" * 72)

    action = decide(
        failures,
        force=args.force,
        interactive=sys.stdin.isatty(),
        check_only=args.check_only,
    )
    if action == "stop":
        return 1 if failures else 0
    if action == "ask":
        if not _ask("Start the server anyway?"):
            return 1

    command = serve_command(args)
    say(f"\n== server\n   $ {' '.join(command)}\n" + "-" * 72)
    try:
        return subprocess.run(command, cwd=ROOT).returncode
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    if shutil.which(sys.executable) is None:  # pragma: no cover
        print("python is not on PATH", file=sys.stderr)
        sys.exit(127)
    sys.exit(main())
