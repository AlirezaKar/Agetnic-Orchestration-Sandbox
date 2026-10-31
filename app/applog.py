"""Per-version runtime logging.

Every release writes to its own file: ``log/runtime/v<version>.log``.
The changelog for each release lives next to it as ``log/v<version>.md``,
indexed in ``log/README.md``.

Logging is strictly best-effort: a read-only checkout or a missing ``log/``
directory must never take the app down.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "log"
RUNTIME_DIR = LOG_DIR / "runtime"

_SAFE = re.compile(r"[^0-9A-Za-z._-]+")


def version_slug(version: str) -> str:
    """``"0.4.0"`` -> ``"0.4.0"``; anything odd is sanitised, never rejected."""
    slug = _SAFE.sub("-", str(version or "unknown")).strip("-.")
    return slug or "unknown"


def runtime_log_path(version: str, base_dir: Path | None = None) -> Path:
    directory = Path(base_dir) if base_dir is not None else RUNTIME_DIR
    return directory / f"v{version_slug(version)}.log"


def append(version: str, message: str, base_dir: Path | None = None) -> Path | None:
    """Append one timestamped line to this version's log.

    Returns the path written, or ``None`` when logging was not possible.
    """
    path = runtime_log_path(version, base_dir)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} [v{version_slug(version)}] {message.rstrip()}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            path.write_text(
                f"# Agetnic Orchestration Sandbox — runtime log for v{version_slug(version)}\n"
                "# one line per event: UTC timestamp, version, message\n",
                encoding="utf-8",
            )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:  # pragma: no cover - depends on filesystem state
        if os.environ.get("AGETNIC_LOG_STRICT"):
            raise
        print(f"[agetnic] could not write {path}: {exc}")
        return None
    return path


def startup_record(version: str, host: str, port: int, ui: str) -> str:
    return (
        f"startup host={host} port={port} ui={ui} "
        f"pid={os.getpid()} python={os.sys.version.split()[0]}"
    )
