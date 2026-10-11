"""Shared contracts: the Pydantic models passed between steps.

Both team members code against these. If a field changes, tell the other person the same day.
Rule: models validate what the LLM returns; invalid output must be handled safely (retry, then fail).
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    blocked = "blocked"
    done = "done"


class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Health(str, Enum):
    on_track = "on_track"
    at_risk = "at_risk"
    off_track = "off_track"


# ---------- step 2: extraction (agent output) ----------
class ExtractedTask(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    owner: Optional[str] = Field(default=None, description="Name as written in the update, or null")
    deadline: Optional[date] = None
    status: Optional[TaskStatus] = Field(default=None, description="Only if the update states a status, else null")
    task_id: Optional[int] = Field(default=None, description="Matched existing task id, or null if new")


class ProjectUpdate(BaseModel):
    project_id: int
    tasks: list[ExtractedTask] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None, max_length=1000)


# ---------- step 3: risk analysis (agent output) ----------
class RiskItem(BaseModel):
    description: str = Field(min_length=5, max_length=500)
    severity: Severity
    is_blocker: bool = False
    related_task_ids: list[int] = Field(default_factory=list)


class RiskReport(BaseModel):
    items: list[RiskItem] = Field(default_factory=list)
    deadline_at_risk: bool = False

    @property
    def is_critical_blocker(self) -> bool:
        """Used by the Python router, never decided by the LLM."""
        return any(i.is_blocker and i.severity == Severity.critical for i in self.items)


# ---------- step 5: worker findings (agent outputs) ----------
class TaskFindings(BaseModel):
    overdue_task_ids: list[int] = Field(default_factory=list)
    blocked_task_ids: list[int] = Field(default_factory=list)
    summary: str = Field(min_length=3, max_length=600)


class PeopleFindings(BaseModel):
    unavailable_member_ids: list[int] = Field(default_factory=list)
    overloaded_member_ids: list[int] = Field(default_factory=list)
    summary: str = Field(min_length=3, max_length=600)


class DeadlineFindings(BaseModel):
    deadline: date
    days_remaining: int
    open_task_count: int = Field(ge=0)
    at_risk: bool
    summary: str = Field(min_length=3, max_length=600)


class Findings(BaseModel):
    """Combined output of the orchestrator and its three workers (step 5)."""
    task: TaskFindings
    people: PeopleFindings
    deadline: DeadlineFindings


# ---------- step 6: report and evaluation ----------
class RecommendedAction(BaseModel):
    action: str = Field(min_length=3, max_length=300)
    task_id: Optional[int] = None


class StatusReport(BaseModel):
    summary: str = Field(min_length=10, max_length=1200)
    overall_health: Health
    blockers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    evidence_task_ids: list[int] = Field(
        default_factory=list, description="Task ids that support the claims in this report"
    )


class EvaluationResult(BaseModel):
    is_grounded: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    revision_needed: bool

    @model_validator(mode="after")
    def _consistent(self) -> "EvaluationResult":
        if not self.is_grounded:
            if not self.unsupported_claims:
                raise ValueError("unsupported_claims is required when is_grounded is false")
            self.revision_needed = True
        return self


# ---------- python-side: routing and approvals ----------
class RouteDecision(BaseModel):
    """Result of the deterministic router (step 4). Produced by Python, not the LLM."""
    needs_human: bool
    reasons: list[str] = Field(default_factory=list)


class ApprovalType(str, Enum):
    critical_risk = "critical_risk"
    deadline_risk = "deadline_risk"
    significant_change = "significant_change"


class ApprovalRequest(BaseModel):
    run_id: str
    type: ApprovalType
    action: str
    payload: dict = Field(default_factory=dict)
    reason: str
