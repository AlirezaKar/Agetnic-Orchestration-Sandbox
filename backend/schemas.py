"""
backend.schemas
===============

Shared Pydantic data-models for the whole project:

* **TaskPlan / TaskSpec** — the structured plan produced by the Planner.
* **AgentResult**          — what every worker agent returns.
* **RunTrace / TraceStep** — the full debug record written to ``traces/*.json``.
* **ChatRequest / ChatResponse** — the HTTP contract used by the test UI.

Keeping these in one module guarantees the Orchestrator, the API layer and
the frontend all agree on a single vocabulary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────
#  Enums
# ──────────────────────────────────────────────────────────────────
class Mode(str, Enum):
    """Manual + automatic routing modes exposed by the UI."""

    AUTO = "auto"
    DOCUMENT = "document"
    WEB_SEARCH = "web_search"
    GENERAL = "general"
    NUMBER = "number"


class AgentName(str, Enum):
    """Worker agents the Manager can delegate to."""

    DOCUMENT = "document"
    WEB_SEARCH = "web_search"
    GENERAL = "general"
    NUMBER = "number"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def utc_now_iso() -> str:
    """Timestamp helper — always timezone-aware ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """Readable unique id, e.g. ``run_9f2c1a0b``."""
    return f"{prefix}_{uuid4().hex[:8]}"


# ──────────────────────────────────────────────────────────────────
#  Planner output
# ──────────────────────────────────────────────────────────────────
class TaskSpec(BaseModel):
    """One unit of work the Manager may delegate to an agent."""

    id: str = Field(description="Short unique id inside the plan, e.g. t1")
    agent: AgentName = Field(description="Which agent executes this task")
    description: str = Field(description="Concrete instruction for the agent")
    depends_on: List[str] = Field(
        default_factory=list,
        description="Ids of tasks whose results feed into this task",
    )
    reason: str = Field(
        default="",
        description="Why this task helps reach the goal (shown in the trace)",
    )


class TaskPlan(BaseModel):
    """The Planner's answer: an ordered, dependency-aware task list."""

    analysis: str = Field(
        default="",
        description="The Planner's short reasoning about the goal",
    )
    tasks: List[TaskSpec] = Field(default_factory=list)

    def waves(self) -> List[List[TaskSpec]]:
        """
        Group tasks into *execution waves*: wave N only depends on tasks
        from earlier waves, so every task inside one wave can run in
        parallel.  Raises ``ValueError`` on cyclic or dangling deps.
        """
        by_id: Dict[str, TaskSpec] = {t.id: t for t in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ValueError("Duplicate task ids in plan")

        resolved: set = set()
        waves: List[List[TaskSpec]] = []
        remaining = list(self.tasks)

        while remaining:
            # Tasks whose dependencies are all satisfied can run now.
            ready = [
                t for t in remaining
                if all(dep in resolved for dep in t.depends_on)
            ]
            if not ready:
                raise ValueError("Cyclic or dangling dependency in plan")
            waves.append(ready)
            resolved.update(t.id for t in ready)
            remaining = [t for t in remaining if t.id not in resolved]

        return waves


# ──────────────────────────────────────────────────────────────────
#  Agent I/O
# ──────────────────────────────────────────────────────────────────
class AgentResult(BaseModel):
    """Uniform result contract returned by every worker agent."""

    agent: AgentName
    success: bool = True
    output: str = ""
    thinking: str = Field(
        default="",
        description="Agent-internal reasoning, kept for the debug trace",
    )
    data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured payload (search hits, retrieved chunks, ...)",
    )
    error: Optional[str] = None
    duration_ms: float = 0.0


class TaskResult(BaseModel):
    """Executor-level record: one TaskSpec after it has been run."""

    task_id: str
    agent: AgentName
    status: TaskStatus = TaskStatus.PENDING
    input: str = ""
    result: Optional[AgentResult] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: float = 0.0
    wave: int = 0
    error: Optional[str] = None


# ──────────────────────────────────────────────────────────────────
#  Trace / debug record  (written to traces/<run_id>.json)
# ──────────────────────────────────────────────────────────────────
class TraceStep(BaseModel):
    """A single visible step in the orchestration timeline."""

    id: str
    type: str = Field(description="e.g. manager_thinking | plan | agent_task | synthesis")
    label: str = ""
    status: TaskStatus = TaskStatus.COMPLETED
    thinking: str = ""
    input: str = ""
    output: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: float = 0.0
    error: Optional[str] = None


class RunTrace(BaseModel):
    """
    Everything about one orchestrated run, persisted as one JSON file.

    The file is the primary artefact for evaluation & debugging: it
    contains the raw *input*, every *thinking* block, the full *process*
    (plan + steps) and the final *output*.
    """

    run_id: str
    session_id: str = ""
    mode_requested: Mode = Mode.AUTO
    mode_used: Mode = Mode.AUTO
    input: str = ""
    output: str = ""
    thinking: List[str] = Field(default_factory=list)
    plan: Optional[TaskPlan] = None
    steps: List[TraceStep] = Field(default_factory=list)
    replan_count: int = 0
    metrics: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    finished_at: Optional[str] = None
    success: bool = True
    error: Optional[str] = None

    def finish(self, output: str, success: bool = True, error: Optional[str] = None) -> None:
        """Stamp the run with its final outcome."""
        self.output = output
        self.success = success
        self.error = error
        self.finished_at = utc_now_iso()


# ──────────────────────────────────────────────────────────────────
#  HTTP contract (test UI ⇄ orchestrator)
# ──────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str  # user | assistant
    content: str
    created_at: str = Field(default_factory=utc_now_iso)


class ChatRequest(BaseModel):
    """One user turn in the test harness."""

    message: str
    mode: Mode = Mode.AUTO
    session_id: str = Field(default_factory=lambda: new_id("sess"))
    # Optional: restrict the auto-mode agents to a subset for experiments.
    allowed_agents: Optional[List[AgentName]] = None


class ChatResponse(BaseModel):
    """Response carrying both the answer and the full orchestration view."""

    run_id: str
    session_id: str
    mode_requested: Mode
    mode_used: Mode
    input: str
    output: str
    thinking: List[str] = Field(default_factory=list)
    plan: Optional[TaskPlan] = None
    steps: List[TraceStep] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    trace_file: str = ""
    success: bool = True
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    llm_provider: str
    web_search_provider: str
    page_fetcher: str = "none"  # crawl4ai | simple_http | none
    models: Dict[str, str]
    trace_dir: str
    document_count: int = 0
    version: str = "1.0.0"
