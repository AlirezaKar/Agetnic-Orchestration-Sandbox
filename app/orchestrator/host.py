"""The only file in this package that touches the host application.

Everything else under ``app/orchestrator/`` imports stdlib + ``app.orchestrator.*``
only; ``tests/test_orchestrator_service.py`` enforces that. The four hooks below
are the whole seam:

| Hook | Default (this repository) | What another project supplies |
|---|---|---|
| :func:`get_logger` | ``app.logs.get_logger`` → `agetnic.orchestrator` in `logs/` | its own logger factory |
| :func:`log_dir` | ``app.logs.log_dir`` → ``logs/orchestrator/`` | where run records may be written |
| :func:`agent_loop` | ``app.agent.WebSearchAgent`` | its own single-agent tool loop |
| :func:`default_settings` | ``app.config.get_settings`` | its own settings factory |

To port the orchestrator: copy ``app/orchestrator/`` and rewrite this file.
Nothing else has to change — the manager, planner, executor, agents, trace and
run log take everything they need as arguments.
"""

from __future__ import annotations

import logging
import os
from typing import Any


def get_logger(name: str = "orchestrator") -> logging.Logger:
    """The logger the orchestrator writes to (host: the ``agetnic`` file logs)."""
    from app import logs  # the host's logging setup — see module docstring

    return logs.get_logger(name or "orchestrator")


def log_dir(path: str | os.PathLike[str] | None = None) -> Any:
    """Where ``runlog`` writes ``runs.jsonl`` + one JSON per run."""
    from app import logs

    return logs.log_dir(path)


def agent_loop() -> Any:
    """The single-agent tool loop the ``search`` agent delegates to."""
    from app.agent import WebSearchAgent

    return WebSearchAgent


def default_settings() -> Any:
    """Fallback settings for direct ``Manager()`` use; the service always injects its own."""
    from app.config import get_settings

    return get_settings()
