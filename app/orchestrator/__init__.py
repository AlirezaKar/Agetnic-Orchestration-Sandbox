"""Manager-led orchestration: plan → parallel specialist agents → synthesis."""

from app.orchestrator.agents import AgentContext, AgentOutcome, catalogue
from app.orchestrator.files import (
    Attachment,
    AttachmentStore,
    FileError,
    decode_payload,
    extract,
)
from app.orchestrator.manager import Manager, OrchestrationResult, agent_catalogue
from app.orchestrator.planner import AGENTS, Plan, PlanStep, heuristic_plan, make_plan

__all__ = [
    "AGENTS",
    "AgentContext",
    "AgentOutcome",
    "Attachment",
    "AttachmentStore",
    "FileError",
    "Manager",
    "OrchestrationResult",
    "Plan",
    "PlanStep",
    "agent_catalogue",
    "catalogue",
    "decode_payload",
    "extract",
    "heuristic_plan",
    "make_plan",
]
