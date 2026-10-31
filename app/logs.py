"""Real program logs: terminal output, errors with tracebacks, request lines, process data.

Everything lands in ``logs/`` (override with ``LOG_DIR``)::

    logs/agetnic.log    everything at LOG_LEVEL and above          (rotating)
    logs/error.log      WARNING + ERROR + tracebacks only          (rotating)
    logs/requests.log   one line per HTTP request                  (rotating)
    logs/terminal.log   raw stdout/stderr of `python run.py` — i.e. what the terminal shows
    logs/process.log    one JSON line per start: pid, cwd, argv, python, platform, git commit
    logs/crash.log      faulthandler output (segfault / hard abort / stack dump)
    logs/archive/       rotated files (agetnic.log.1, terminal-2026…log, …)
    logs/changelog/     release notes, one markdown file per version (committed)

Design notes:

* **Never break the app.** Every filesystem operation is best-effort: a read-only
  checkout or a full disk downgrades to console logging instead of raising.
* **No secrets in logs.** Keys matching ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET``
  are collected from the environment and their literal values are replaced with
  ``«redacted»``; common ``sk-…`` / ``Bearer …`` shapes are scrubbed too.
* **Idempotent.** ``setup_logging()`` is safe to call from ``run.py``, from
  ``app.main`` at import time, and from the scripts; the second call is a no-op
  unless ``force=True``.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import faulthandler
import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, TextIO

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
REDACTED = "«redacted»"

FILE_FORMAT = "%(asctime)s %(levelname)-8s pid=%(process)d %(name)s: %(message)s"
CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# A crash dump must not blow up; keep it small.
_FAULT_FILE: TextIO | None = None
_CONFIGURED = False
_STARTUP_RECORDED = False
_LOGGERS_WITH_FILE_HANDLERS: set[str] = set()

# --------------------------------------------------------------------- paths


def log_dir(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the log directory: explicit argument → ``LOG_DIR`` → ``<repo>/logs``."""
    if path is not None:
        return Path(path)
    env = os.environ.get("LOG_DIR", "").strip()
    return Path(env) if env else DEFAULT_LOG_DIR


def log_paths(directory: Path | None = None) -> dict[str, Path]:
    base = log_dir(directory)
    return {
        "dir": base,
        "app": base / "agetnic.log",
        "errors": base / "error.log",
        "requests": base / "requests.log",
        "terminal": base / "terminal.log",
        "process": base / "process.log",
        "crash": base / "crash.log",
        "archive": base / "archive",
        "changelog": base / "changelog",
    }


# ------------------------------------------------------------------ redaction

_SECRET_ENV = re.compile(r"(API_KEY|APIKEY|_TOKEN|TOKEN$|SECRET|PASSWORD)", re.IGNORECASE)
_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),  # OpenAI / OpenRouter style
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{8,}"),  # Groq
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}"),  # Google
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[-_]?key|token|secret|password)\b\s*[:=]\s*\S+"),
)


def secret_values() -> list[str]:
    """Values from the environment that must never reach a log line."""
    found: list[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if _SECRET_ENV.search(name):
            found.append(value)
    return found


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    """Scrub credentials from ``text``. Safe on arbitrary strings."""
    if not text:
        return text
    out = text
    for value in secrets if secrets is not None else secret_values():
        if value and len(value) >= 6 and value in out:
            out = out.replace(value, REDACTED)
    out = _SHAPES[0].sub("sk-" + REDACTED, out)
    out = _SHAPES[1].sub("gsk_" + REDACTED, out)
    out = _SHAPES[2].sub("AIza" + REDACTED, out)
    out = _SHAPES[3].sub(lambda m: m.group(1) + " " + REDACTED, out)
    out = _SHAPES[4].sub(lambda m: m.group(1) + "=" + REDACTED, out)
    return out


class RedactingFormatter(logging.Formatter):
    """Formats first, then redacts — so tracebacks and args are covered too."""

    def __init__(self, fmt: str, datefmt: str = DATE_FORMAT) -> None:
        super().__init__(fmt, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


# -------------------------------------------------------------------- helpers


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, "") or default), 0)
    except (TypeError, ValueError):
        return default


def _level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    name = str(value or os.environ.get("LOG_LEVEL") or "INFO").upper()
    return getattr(logging, name, logging.INFO) if not name.isdigit() else int(name)


def get_logger(name: str = "") -> logging.Logger:
    """``get_logger("api")`` → the ``agetnic.api`` logger (console + all files)."""
    return logging.getLogger(f"agetnic.{name}" if name else "agetnic")


# ------------------------------------------------------------------ handlers


def _rotating(path: Path, level: int, fmt: str, rotation_mb: float, backups: int):
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=int(rotation_mb * 1024 * 1024),
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(RedactingFormatter(fmt))
    return handler


def _safe_mkdir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def setup_logging(
    directory: str | os.PathLike[str] | None = None,
    level: str | int | None = None,
    rotation_mb: float | None = None,
    backups: int | None = None,
    force: bool = False,
    console: bool = True,
) -> dict[str, Any]:
    """Configure console + file logging once. Returns a summary dict.

    ``{"dir": Path, "files": {...}, "level": "INFO", "writable": True}``
    """
    global _CONFIGURED

    paths = log_paths(Path(directory) if directory is not None else None)
    resolved_level = _level(level)
    rotation = _env_float("LOG_ROTATE_MB", 5.0) if rotation_mb is None else rotation_mb
    keep = _env_int("LOG_BACKUPS", 5) if backups is None else backups

    summary: dict[str, Any] = {
        "dir": paths["dir"],
        "files": {k: str(v) for k, v in paths.items() if k not in ("dir", "archive", "changelog")},
        "archive": str(paths["archive"]),
        "level": logging.getLevelName(resolved_level),
        "writable": False,
    }

    root = logging.getLogger()
    if _CONFIGURED and not force:
        summary["writable"] = any(
            isinstance(h, logging.FileHandler) for h in root.handlers
        )
        return summary
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive
                pass
        _LOGGERS_WITH_FILE_HANDLERS.clear()

    root.setLevel(resolved_level)

    if console and not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(resolved_level)
        stream.setFormatter(RedactingFormatter(CONSOLE_FORMAT))
        root.addHandler(stream)

    if not _safe_mkdir(paths["dir"]) or not _safe_mkdir(paths["archive"]):
        summary["writable"] = False
        _CONFIGURED = True
        print(f"[agetnic] logs are not writable at {paths['dir']}; logging to the console only")
        return summary

    summary["writable"] = True
    root.addHandler(_rotating(paths["app"], resolved_level, FILE_FORMAT, rotation, keep))
    error_level = max(resolved_level, logging.WARNING)
    root.addHandler(_rotating(paths["errors"], error_level, FILE_FORMAT, rotation, keep))

    # A dedicated logger so requests.log exists even before the first request,
    # and so its rotation/level can diverge from the app log. Handlers left from
    # a previous configuration (other LOG_DIR, tests) are dropped, otherwise
    # lines would keep going to a file that is no longer the current one.
    requests_logger = logging.getLogger("agetnic.requests")
    for handler in list(requests_logger.handlers):
        requests_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass
    requests_logger.setLevel(resolved_level)
    requests_logger.propagate = True  # also lands in agetnic.log / error.log
    requests_logger.addHandler(
        _rotating(paths["requests"], resolved_level, FILE_FORMAT, rotation, keep)
    )
    _LOGGERS_WITH_FILE_HANDLERS.add("agetnic.requests")

    _CONFIGURED = True
    return summary


# ------------------------------------------------------------- terminal tees


class Tee:
    """Mirror a stream into a file, keeping it usable as ``sys.stdout``/``sys.stderr``."""

    def __init__(self, stream: TextIO, file: TextIO) -> None:
        self._stream = stream
        self._file = file

    def write(self, data: str) -> int:
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:  # pragma: no cover - disk full / closed
            pass
        return self._stream.write(data)

    def flush(self) -> None:
        try:
            self._file.flush()
        except Exception:  # pragma: no cover
            pass
        try:
            self._stream.flush()
        except Exception:  # pragma: no cover
            pass

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self._stream, "errors", "replace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _rotate_raw(path: Path, archive: Path, limit_mb: float, keep: int) -> None:
    """Move an oversized raw log (terminal.log) into ``archive/`` at startup."""
    try:
        if not path.exists() or path.stat().st_size < limit_mb * 1024 * 1024:
            return
        archive.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path.replace(archive / f"{path.stem}-{stamp}{path.suffix}")
        old = sorted(archive.glob(f"{path.stem}-*{path.suffix}"))
        for extra in old[: max(len(old) - keep, 0)]:
            extra.unlink(missing_ok=True)
    except OSError:
        pass


def capture_terminal(directory: str | os.PathLike[str] | None = None, limit_mb: float = 10.0, keep: int = 5) -> Path | None:
    """Tee ``sys.stdout``/``sys.stderr`` into ``logs/terminal.log``.

    Call this **before** :func:`setup_logging` so the console handler ends up in
    the same file as the app's own prints and uvicorn's output.
    """
    paths = log_paths(Path(directory) if directory is not None else None)
    if not _safe_mkdir(paths["dir"]):
        return None
    _rotate_raw(paths["terminal"], paths["archive"], limit_mb, keep)
    if isinstance(sys.stdout, Tee) or isinstance(sys.stderr, Tee):  # already captured
        return paths["terminal"]
    try:
        handle = paths["terminal"].open("a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = Tee(sys.stdout, handle)  # type: ignore[assignment]
    sys.stderr = Tee(sys.stderr, handle)  # type: ignore[assignment]
    atexit.register(handle.close)
    return paths["terminal"]


# ------------------------------------------------------------- process data


def _git_commit() -> str:
    head = REPO_ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref = ref[5:]
            resolved = (REPO_ROOT / ".git" / ref).read_text(encoding="utf-8").strip()
        else:
            resolved = ref
        return resolved[:12]
    except OSError:
        return "unknown"


def process_snapshot(version: str | None = None, **extra: Any) -> dict[str, Any]:
    """Process + host data recorded on every start (no secrets)."""
    snapshot: dict[str, Any] = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "version": version,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "argv": sys.argv,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "threads": threading.active_count(),
        "git": _git_commit(),
        "log_dir": str(log_dir()),
    }
    try:
        import resource  # noqa: PLC0415 - unix only

        snapshot["max_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # pragma: no cover - windows / restricted
        pass
    snapshot.update(extra)
    return snapshot


def startup_recorded() -> bool:
    """True once :func:`record_startup` ran in this process (avoids duplicates)."""
    return _STARTUP_RECORDED


def record_startup(version: str, **extra: Any) -> dict[str, Any]:
    """Append one JSON line to ``logs/process.log`` and mirror a summary to agetnic.log."""
    global _STARTUP_RECORDED
    _STARTUP_RECORDED = True
    snapshot = process_snapshot(version, **extra)
    paths = log_paths()
    try:
        if _safe_mkdir(paths["dir"]):
            with paths["process"].open("a", encoding="utf-8") as handle:
                handle.write(redact(json.dumps(snapshot, default=str)) + "\n")
    except OSError:  # pragma: no cover - best effort
        pass
    get_logger("process").info(
        "start version=%s pid=%s port=%s cwd=%s git=%s",
        version,
        snapshot["pid"],
        extra.get("port", "-"),
        snapshot["cwd"],
        snapshot["git"],
    )
    return snapshot


def install_exception_hooks(directory: str | os.PathLike[str] | None = None) -> None:
    """Log uncaught exceptions from the main thread, threads and asyncio loops."""
    logger = get_logger("crash")
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        logger.critical(
            "uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook


def enable_faulthandler(directory: str | os.PathLike[str] | None = None) -> Path | None:
    """Keep ``logs/crash.log`` open for faulthandler (segfaults, hard aborts)."""
    global _FAULT_FILE
    paths = log_paths(Path(directory) if directory is not None else None)
    if _FAULT_FILE is not None:
        return paths["crash"]
    if not _safe_mkdir(paths["dir"]):
        return None
    try:
        _FAULT_FILE = paths["crash"].open("a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        _FAULT_FILE = None
        return None
    return paths["crash"]


# --------------------------------------------------------------- HTTP access


def _client(request) -> str:
    """``127.0.0.1:54321`` for both Starlette requests and raw ASGI scopes."""
    client = getattr(request, "client", None)
    if not client:
        return "-"
    if isinstance(client, (tuple, list)) and len(client) >= 2:
        return f"{client[0]}:{client[1]}"
    host = getattr(client, "host", None) or str(client)
    port = getattr(client, "port", None)
    return f"{host}:{port}" if port else str(host)


def log_request(request, status: int, duration_ms: float, request_id: str, bytes_out: int = 0) -> None:
    """One line in ``requests.log`` (and in agetnic.log / error.log when it fails).

    Works with a Starlette ``Request`` and with the light :class:`_ScopeRequest`
    shim the middleware uses.
    """
    url = getattr(request, "url", None)
    path = getattr(request, "path", None) or getattr(url, "path", "-")
    query = getattr(request, "query", None)
    if query is None:
        query = getattr(url, "query", "") or ""
    target = f"{path}?{query}" if query else str(path)
    line = "%s %s -> %s %.1fms req=%s client=%s bytes=%s"
    args = (
        getattr(request, "method", "-"),
        target,
        status,
        duration_ms,
        request_id,
        _client(request),
        bytes_out,
    )
    logger = get_logger("requests")
    if status >= 500:
        logger.error(line, *args)
    elif status >= 400:
        logger.warning(line, *args)
    else:
        logger.info(line, *args)


class RequestLoggingMiddleware:
    """ASGI middleware: timing + status + request id for every request.

    Import-light on purpose (no Starlette import) so it can wrap any ASGI app,
    including the small apps used in the tests.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        import uuid

        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                request_id = value.decode("latin-1")[:32]
                break
        request_id = request_id or uuid.uuid4().hex[:10]

        started = time.perf_counter()
        status = 500
        sent = 0

        async def _send(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b"") or b"")
            await send(message)

        scope.setdefault("state", {})["request_id"] = request_id
        try:
            await self.app(scope, receive, _send)
        except Exception:
            duration = (time.perf_counter() - started) * 1000.0
            shim = _ScopeRequest(scope)
            # the traceback belongs in error.log, not in the one-line request log
            get_logger("crash").exception(
                "unhandled exception on %s %s req=%s",
                shim.method,
                shim.path,
                request_id,
            )
            get_logger("requests").error(
                "%s %s -> 500 %.1fms req=%s client=%s (traceback in error.log)",
                shim.method,
                shim.path,
                duration,
                request_id,
                _client(shim),
            )
            raise
        else:
            duration = (time.perf_counter() - started) * 1000.0
            log_request(_ScopeRequest(scope), status, duration, request_id, sent)


class _ScopeRequest:
    """Minimal request shim for :func:`log_request` (works without Starlette)."""

    def __init__(self, scope: dict[str, Any]) -> None:
        self.method = scope.get("method", "-")
        self.path = scope.get("path", "-")
        self.query = ""
        raw = scope.get("query_string", b"") or b""
        try:
            self.query = raw.decode("latin-1")
        except Exception:  # pragma: no cover
            self.query = ""
        self.client = scope.get("client")


# ------------------------------------------------------------------ reading


def list_logs(directory: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """``[{"name", "path", "bytes", "modified"}]`` for every file in the log dir."""
    base = log_dir(directory)
    entries: list[dict[str, Any]] = []
    if not base.exists():
        return entries
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover
            continue
        entries.append(
            {
                "name": str(path.relative_to(base)),
                "path": path,
                "bytes": stat.st_size,
                "modified": _dt.datetime.fromtimestamp(stat.st_mtime),
            }
        )
    return entries


def resolve_log_file(name: str, directory: str | os.PathLike[str] | None = None) -> Path:
    """Map ``error``/``error.log``/``requests`` to a real file inside the log dir."""
    base = log_dir(directory)
    candidate = Path(name)
    if candidate.is_absolute():
        path = candidate
    elif candidate.suffix:
        path = base / candidate
    else:
        path = base / f"{name}.log"
    # never escape the log directory
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"{name!r} is outside the log directory {base}") from exc
    return path


def tail(path: Path | str, lines: int = 50) -> list[str]:
    """Last ``lines`` lines of a file, cheaply (reads the tail of large files)."""
    target = Path(path)
    if not target.exists():
        return []
    lines = max(int(lines), 1)
    block = 8192
    with target.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            handle.seek(size)
            data = handle.read(step) + data
    return data.decode("utf-8", "replace").splitlines()[-lines:]


def follow(path: Path | str, poll: float = 0.4):
    """Yield appended lines as they are written (used by ``scripts.logs --follow``)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                yield line.rstrip("\n")
            else:
                time.sleep(poll)


def read_errors(path: Path | str, count: int = 0, limit: int = 20) -> list[str]:
    """Error records from ``error.log`` (a record starts at each timestamped line)."""
    target = Path(path)
    if not target.exists():
        return []
    records: list[list[str]] = []
    stamp = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        if stamp.match(line):
            if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (WARNING|ERROR|CRITICAL)", line):
                records.append([line])
            else:
                if records:
                    records[-1].append(line)
        elif records:
            records[-1].append(line)
    if count:
        records = records[-count:]
    return ["\n".join(record) for record in records[-limit:]]
