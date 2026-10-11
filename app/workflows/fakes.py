"""Fake agents with canned answers. Used by the tests, and by the teammate to build the UI without an API key."""
from __future__ import annotations

from typing import Any, Optional

from app.models import (DeadlineFindings, EvaluationResult, Findings, Health, PeopleFindings, ProjectUpdate,
                        RiskReport, StatusReport, TaskFindings)
from app.workflows.project_update import Agents

from datetime import date


def default_findings() -> Findings:
    return Findings(
        task=TaskFindings(overdue_task_ids=[], blocked_task_ids=[], summary="No overdue or blocked tasks."),
        people=PeopleFindings(summary="Team is available."),
        deadline=DeadlineFindings(deadline=date(2026, 11, 25), days_remaining=45, open_task_count=3, at_risk=False,
                                  summary="Plenty of time left."),
    )


def good_report(**kw) -> StatusReport:
    base = dict(summary="Website redesign is on track with no blockers.", overall_health=Health.on_track,
                evidence_task_ids=[11])
    base.update(kw)
    return StatusReport(**base)


class FakeAgents:
    """Each argument is a value or a list of values (used in order; the last one repeats).
    An Exception instance in a list is raised instead of returned."""

    def __init__(self, update: Any = None, risk: Any = None, findings: Any = None,
                 drafts: Any = None, evaluations: Any = None):
        self.queues = {
            "extract": self._q(update if update is not None else ProjectUpdate(project_id=2)),
            "risk": self._q(risk if risk is not None else RiskReport()),
            "gather": self._q(findings if findings is not None else default_findings()),
            "write": self._q(drafts if drafts is not None else good_report()),
            "evaluate": self._q(evaluations if evaluations is not None
                                else EvaluationResult(is_grounded=True, revision_needed=False)),
        }
        self.calls = {k: 0 for k in self.queues}
        self.feedback_seen: list[list[str]] = []

    @staticmethod
    def _q(v: Any) -> list:
        return list(v) if isinstance(v, (list, tuple)) else [v]

    def _next(self, name: str) -> Any:
        q = self.queues[name]
        item = q[min(self.calls[name], len(q) - 1)]
        self.calls[name] += 1
        if isinstance(item, Exception):
            raise item
        return item

    def as_agents(self) -> Agents:
        async def extract(text, project_id, ctx): return self._next("extract")
        async def risk(update, ctx): return self._next("risk")
        async def gather(update, risk_report, ctx): return self._next("gather")
        async def write(findings, update, risk_report, feedback, ctx):
            self.feedback_seen.append(feedback)
            return self._next("write")
        async def evaluate(report, findings, ctx): return self._next("evaluate")
        return Agents(extract, risk, gather, write, evaluate)
