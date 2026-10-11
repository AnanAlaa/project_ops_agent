"""WF-01 Project Update Processing: the pipeline from the architecture diagram.

CODE decides (this file): input guardrail, routing, retry/stop rules, the evaluator loop limit, output guardrail,
which tool calls run. The LLM agents are plugged in through the `Agents` interface below, so the pipeline can be
tested with fakes (app/workflows/fakes.py) and built with the real agents by the teammate (app/agents/).

Agent interface (each is an async function returning a validated Pydantic model):
    extract(text, project_id, ctx)                  -> ProjectUpdate     step 2  (LLM)
    analyze_risk(update, ctx)                       -> RiskReport        step 3  (LLM)
    gather(update, risk, ctx)                       -> Findings          step 5  (LLM orchestrator + 3 parallel workers)
    write(findings, update, risk, feedback, ctx)    -> StatusReport      step 6a (LLM)
    evaluate(report, findings, ctx)                 -> EvaluationResult  step 6b (LLM)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from app import guardrails, rules, tools
from app.approvals import create_approval
from app.context import ToolContext
from app.models import (EvaluationResult, Findings, ProjectUpdate, RiskReport, RouteDecision, StatusReport)
from app.tracing import workflow_trace

MAX_REVISION_ROUNDS = 3
DEFAULT_BACKOFF = (1, 2, 4)      # seconds between retries after a timeout, 429 or 5xx


@dataclass
class Agents:
    extract: Callable[..., Awaitable[ProjectUpdate]]
    analyze_risk: Callable[..., Awaitable[RiskReport]]
    gather: Callable[..., Awaitable[Findings]]
    write: Callable[..., Awaitable[StatusReport]]
    evaluate: Callable[..., Awaitable[EvaluationResult]]


@dataclass
class RunResult:
    status: str                  # blocked_input | held_for_approval | completed | needs_review | rejected_output | error
    message: str
    report: Optional[StatusReport] = None
    update: Optional[ProjectUpdate] = None
    risk: Optional[RiskReport] = None
    route: Optional[RouteDecision] = None
    findings: Optional[Findings] = None
    actions: list[dict] = field(default_factory=list)
    approval_ids: list[int] = field(default_factory=list)
    revisions: int = 0
    violations: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)     # readable step log for the UI and the demo


class StepFailed(Exception):
    def __init__(self, step: str, cause: Exception):
        super().__init__(f"{step} failed: {cause}")
        self.step = step


async def call_with_retry(step: str, fn: Callable[..., Awaitable[Any]], *args: Any,
                          backoff: tuple = DEFAULT_BACKOFF) -> Any:
    """Invalid structured output: retry once. Any other failure (timeout, 429, 5xx): retry with backoff.
    After that the step fails and the workflow stops safely."""
    validation_retries, other_retries = 1, list(backoff)
    while True:
        try:
            return await fn(*args)
        except ValidationError as e:
            if validation_retries == 0:
                raise StepFailed(step, e)
            validation_retries -= 1
        except Exception as e:
            if not other_retries:
                raise StepFailed(step, e)
            await asyncio.sleep(other_retries.pop(0))


# ---------- step 8: turn the extracted update into tool calls (Python decides what runs) ----------
def _outcome(result: dict) -> str:
    if "error" in result:
        return "error"
    if result.get("blocked"):
        return "blocked"
    if "pending_approval" in result:
        return "pending_approval"
    return "applied"


def apply_update_actions(ctx: ToolContext, project_id: int, update: ProjectUpdate) -> list[dict]:
    """Every action is reported with what the tool actually returned. Never claim more than that."""
    actions: list[dict] = []
    members = {r["name"].lower(): r["id"] for r in ctx.conn.execute("SELECT id, name FROM team_members")}

    def record(kind: str, task_ref: Any, result: dict) -> dict:
        a = {"action": kind, "task": task_ref, "outcome": _outcome(result),
             "detail": result.get("error") or result.get("reason") or ""}
        if "pending_approval" in result:
            a["approval_id"] = result["pending_approval"]
        actions.append(a)
        return a

    for t in update.tasks:
        owner_id = members.get(t.owner.lower()) if t.owner else None
        if t.owner and owner_id is None:
            actions.append({"action": "assign", "task": t.title, "outcome": "skipped",
                            "detail": f"Owner '{t.owner}' is not a team member."})
        due = t.deadline.isoformat() if t.deadline else None
        if t.task_id is None:
            record("create", t.title, tools.create_task(ctx, project_id, t.title, due, "medium", owner_id))
            continue
        row = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (t.task_id,)).fetchone()
        if row is None or row["project_id"] != project_id:
            actions.append({"action": "update", "task": t.task_id, "outcome": "error",
                            "detail": "Task not found in this project."})
            continue
        if t.status is not None and t.status.value != row["status"]:
            record("update_status", t.task_id, tools.update_task(ctx, t.task_id, status=t.status.value))
        if due and due != row["due_date"]:
            record("update_due_date", t.task_id, tools.update_task(ctx, t.task_id, due_date=due))
        if owner_id is not None and owner_id != row["owner_id"]:
            record("assign", t.task_id, tools.assign_task(ctx, t.task_id, owner_id))
    return actions


# ---------- the workflow ----------
async def run_project_update(ctx: ToolContext, project_id: int, text: str, agents: Agents,
                             max_rounds: int = MAX_REVISION_ROUNDS,
                             backoff: tuple = DEFAULT_BACKOFF) -> RunResult:
    steps: list[str] = []
    res = RunResult(status="error", message="", steps=steps)

    def log(msg: str) -> None:
        steps.append(msg)
        tools.audit(ctx, "workflow", "step", msg, step="WF-01")

    with workflow_trace("WF-01 project update"):
        # 1. input guardrail (CODE)
        g = guardrails.check_input(text)
        if not g.allowed:
            log(f"1 input guardrail: BLOCKED ({g.category})")
            res.status, res.message = "blocked_input", g.reason
            return res
        log("1 input guardrail: passed")

        try:
            # 2. task extraction (LLM)
            res.update = await call_with_retry("task extraction", agents.extract, text, project_id, ctx, backoff=backoff)
            log(f"2 extraction: {len(res.update.tasks)} task(s) found")

            # 3. risk analysis (LLM), chained on step 2
            res.risk = await call_with_retry("risk analysis", agents.analyze_risk, res.update, ctx, backoff=backoff)
            log(f"3 risk analysis: {len(res.risk.items)} risk(s)")

            # 4. router (CODE)
            res.route = rules.route_update(ctx, project_id, res.risk)
            if res.route.needs_human:
                kind = "critical_risk" if any("critical" in r for r in res.route.reasons) else "deadline_risk"
                aid = create_approval(ctx, kind, "Review project update before any action",
                                      {"project_id": project_id, "update": res.update.model_dump(mode="json"),
                                       "risk": res.risk.model_dump(mode="json")}, " ".join(res.route.reasons))
                res.approval_ids.append(aid)
                log("4 router: HELD for human approval - " + "; ".join(res.route.reasons))
                res.status = "held_for_approval"
                res.message = "A person must review this before anything is done. No changes were made."
                return res
            log("4 router: no human review needed")

            # 5. orchestrator + parallel workers (LLM)
            res.findings = await call_with_retry("project analysis", agents.gather, res.update, res.risk, ctx, backoff=backoff)
            log("5 orchestrator and workers: findings collected")

            # 6. writer <-> evaluator loop, bounded (LLM + CODE)
            feedback: list[str] = []
            guard = None
            evaluation = None
            for round_no in range(1, max_rounds + 1):
                res.report = await call_with_retry("report writing", agents.write, res.findings, res.update,
                                                   res.risk, list(feedback), ctx, backoff=backoff)
                guard = guardrails.check_output(res.report, ctx)          # 7. output guardrail (CODE)
                if not guard.allowed:
                    feedback = guard.violations
                    log(f"6.{round_no} draft rejected by output guardrail: {guard.violations}")
                    continue
                evaluation = await call_with_retry("report evaluation", agents.evaluate, res.report, res.findings,
                                                   ctx, backoff=backoff)
                if evaluation.is_grounded and not evaluation.revision_needed:
                    log(f"6.{round_no} evaluator approved the report")
                    break
                feedback = evaluation.unsupported_claims
                log(f"6.{round_no} evaluator asked for a revision: {feedback}")
            res.revisions = max(0, round_no - 1)

            approved = guard is not None and guard.allowed and evaluation is not None \
                and evaluation.is_grounded and not evaluation.revision_needed
            if not approved:
                if guard is not None and not guard.allowed:
                    res.status, res.report = "rejected_output", None
                    res.violations = guard.violations
                    res.message = "The report was rejected because it contained unsupported statements."
                else:
                    res.status = "needs_review"
                    res.violations = list(feedback)
                    res.message = f"The report could not be fully verified after {max_rounds} rounds. Human review needed."
                log(f"6 result: {res.status}")
                return res

            # 8. actions (CODE)
            res.actions = apply_update_actions(ctx, project_id, res.update)
            res.approval_ids += [a["approval_id"] for a in res.actions if "approval_id" in a]
            done = sum(a["outcome"] == "applied" for a in res.actions)
            pending = sum(a["outcome"] == "pending_approval" for a in res.actions)
            blocked = sum(a["outcome"] in ("blocked", "error") for a in res.actions)
            log(f"8 actions: {done} applied, {pending} waiting for approval, {blocked} blocked or failed")
            res.status = "completed"
            res.message = "Status report ready."
            return res

        except StepFailed as e:
            tools.audit(ctx, "workflow", "error", str(e), step="WF-ERR")
            steps.append(f"ERROR in {e.step}")
            res.status = "error"
            res.message = f"Something went wrong during {e.step}. Nothing was changed. Please try again."
            return res
