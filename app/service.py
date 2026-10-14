"""The glue between the UI and the workflows.

Thread-safe: every function opens and closes its own SQLite connection.
Supports both Live Mode (OpenAI Agents) and Demo Mode (offline smart simulator).
"""
from __future__ import annotations

import asyncio
from datetime import date
import os
import re
from typing import Any, Optional
import uuid

from app import approvals as approvals_mod
from app import tools
from app.context import ROLES, ToolContext
from app.db import get_connection, init_db, seed
from app.models import (
    DeadlineFindings, EvaluationResult, ExtractedTask, Findings, Health,
    PeopleFindings, Priority, ProjectUpdate, RecommendedAction, RiskItem,
    RiskReport, Severity, StatusReport, TaskFindings, TaskStatus
)
from app.workflows.project_update import (Agents, RunResult, run_project_update)

STATUS_TEXT = {
    "completed": ("OK", "Report ready"),
    "held_for_approval": ("HELD", "Held for human approval - nothing was changed"),
    "blocked_input": ("BLOCKED", "Blocked by the input guardrail - nothing was changed"),
    "needs_review": ("REVIEW", "Needs human review - the report could not be verified"),
    "rejected_output": ("REJECTED", "Report rejected by the output guardrail"),
    "error": ("ERROR", "Something went wrong - nothing was changed"),
}


def ensure_database(path: Optional[str] = None) -> None:
    """Create tables and synthetic demo data if database is uninitialized."""
    conn = get_connection(path)
    try:
        init_db(conn)
        count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if count == 0:
            seed(conn)
    finally:
        conn.close()


def reset_demo_data(path: Optional[str] = None) -> None:
    """Reset the database to clean seed state."""
    conn = get_connection(path)
    try:
        init_db(conn)
        seed(conn)
    finally:
        conn.close()


def has_api_key() -> bool:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    return bool(key and key != "your-key-here")


def agent_mode() -> str:
    forced = os.getenv("FAKE_AGENTS", "").strip().lower() in ("1", "true", "yes")
    if forced or not has_api_key():
        return "demo"
    return "live"


def _role(role: str) -> str:
    return role if role in ROLES else "viewer"


def new_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:10]}"


def list_projects() -> list[tuple[str, int]]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
        return [(f"{r['id']} - {r['name']}", r["id"]) for r in rows]
    finally:
        conn.close()


# ---------- Smart Simulated Agents for Demo Mode ----------
def build_demo_agents(project_id: int, text: str = "") -> Agents:
    """Build dynamic agents for demo mode that intelligently respond to input text and database state."""

    async def extract(input_text: str, pid: int, ctx: ToolContext) -> ProjectUpdate:
        tasks: list[ExtractedTask] = []
        low = (input_text or "").lower()

        # Check existing tasks in database for matches
        db_tasks = ctx.conn.execute(
            "SELECT id, title, owner_id FROM tasks WHERE project_id = ?", (pid,)
        ).fetchall()

        matched_ids = set()
        for t in db_tasks:
            t_title = t["title"].lower()
            # If significant title words match
            if any(word in low for word in t_title.split() if len(word) > 4):
                matched_ids.add(t["id"])
                # Infer status from text if mentioned
                status = None
                if any(w in low for w in ("done", "finished", "completed")):
                    status = TaskStatus.done
                elif any(w in low for w in ("blocked", "stuck")):
                    status = TaskStatus.blocked
                elif any(w in low for w in ("in progress", "started", "working")):
                    status = TaskStatus.in_progress

                tasks.append(ExtractedTask(
                    title=t["title"],
                    task_id=t["id"],
                    status=status
                ))

        # Check for new tasks requested in text: "new task: <title>" or "create task <title>"
        create_match = re.search(r"(?:new task|create task)[:\s]+([^.,\n]+)", input_text, re.IGNORECASE)
        if create_match:
            new_title = create_match.group(1).strip()
            if len(new_title) >= 3:
                tasks.append(ExtractedTask(title=new_title, task_id=None, status=TaskStatus.todo))

        # Fallback if no specific task matched
        if not tasks and db_tasks:
            first = db_tasks[0]
            tasks.append(ExtractedTask(title=first["title"], task_id=first["id"]))

        return ProjectUpdate(project_id=pid, tasks=tasks, notes=f"Extracted from update: {input_text[:100]}...")

    async def analyze_risk(update: ProjectUpdate, ctx: ToolContext) -> RiskReport:
        items: list[RiskItem] = []
        low = text.lower()
        deadline_at_risk = False

        if any(w in low for w in ("critical", "blocker", "emergency", "halt", "down")):
            items.append(RiskItem(
                description="Critical issue reported in update text.",
                severity=Severity.critical,
                is_blocker=True,
                related_task_ids=[t.task_id for t in update.tasks if t.task_id]
            ))
        elif any(w in low for w in ("delay", "overdue", "late", "behind")):
            items.append(RiskItem(
                description="Moderate timeline delay noted in update.",
                severity=Severity.medium,
                is_blocker=False,
                related_task_ids=[t.task_id for t in update.tasks if t.task_id]
            ))
            deadline_at_risk = True

        return RiskReport(items=items, deadline_at_risk=deadline_at_risk)

    async def gather(update: ProjectUpdate, risk_report: RiskReport, ctx: ToolContext) -> Findings:
        # Query true facts from DB
        overdue_rows = ctx.conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND due_date < ? AND status != 'done'",
            (project_id, ctx.today.isoformat()),
        ).fetchall()
        blocked_rows = ctx.conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND status = 'blocked'",
            (project_id,),
        ).fetchall()
        overdue_ids = [r["id"] for r in overdue_rows]
        blocked_ids = [r["id"] for r in blocked_rows]

        p = ctx.conn.execute("SELECT deadline FROM projects WHERE id = ?", (project_id,)).fetchone()
        deadline_date = date.fromisoformat(p["deadline"]) if p else ctx.today
        days_left = (deadline_date - ctx.today).days
        open_count = ctx.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status != 'done'",
            (project_id,),
        ).fetchone()[0]

        # People findings
        unavail = [r["id"] for r in ctx.conn.execute("SELECT id FROM team_members WHERE available = 0").fetchall()]
        overloaded = [r["id"] for r in ctx.conn.execute("SELECT id FROM team_members WHERE assigned_hours > capacity_hours").fetchall()]

        return Findings(
            task=TaskFindings(
                overdue_task_ids=overdue_ids,
                blocked_task_ids=blocked_ids,
                summary=f"{len(overdue_ids)} overdue task(s), {len(blocked_ids)} blocked task(s)."
            ),
            people=PeopleFindings(
                unavailable_member_ids=unavail,
                overloaded_member_ids=overloaded,
                summary=f"{len(unavail)} member(s) unavailable, {len(overloaded)} overloaded."
            ),
            deadline=DeadlineFindings(
                deadline=deadline_date,
                days_remaining=days_left,
                open_task_count=open_count,
                at_risk=days_left < 7 and (bool(blocked_ids) or bool(overdue_ids)),
                summary=f"Deadline is {deadline_date} ({days_left} days left) with {open_count} open task(s)."
            )
        )

    async def write(findings: Findings, update: ProjectUpdate, risk_report: RiskReport,
                    feedback: list[str], ctx: ToolContext) -> StatusReport:
        # Find real existing task IDs for evidence
        ev_rows = ctx.conn.execute("SELECT id FROM tasks WHERE project_id = ? LIMIT 3", (project_id,)).fetchall()
        evidence = [r["id"] for r in ev_rows]

        health = Health.on_track
        if findings.task.blocked_task_ids or findings.deadline.at_risk or risk_report.is_critical_blocker:
            health = Health.at_risk if not risk_report.is_critical_blocker else Health.off_track

        recs = []
        if findings.task.blocked_task_ids:
            recs.append(RecommendedAction(
                action="Resolve blocker with external dependencies.",
                task_id=findings.task.blocked_task_ids[0]
            ))
        if findings.people.overloaded_member_ids:
            recs.append(RecommendedAction(action="Rebalance workload away from overloaded members."))

        return StatusReport(
            summary=f"Project {project_id} analysis completed. Work is {health.value.replace('_', ' ')}.",
            overall_health=health,
            blockers=[f"Blocked tasks: {findings.task.blocked_task_ids}"] if findings.task.blocked_task_ids else [],
            risks=[item.description for item in risk_report.items],
            recommended_actions=recs,
            evidence_task_ids=evidence
        )

    async def evaluate(report: StatusReport, findings: Findings, ctx: ToolContext) -> EvaluationResult:
        return EvaluationResult(is_grounded=True, revision_needed=False)

    return Agents(extract, analyze_risk, gather, write, evaluate)


def get_agents(project_id: int = 2, text: str = "") -> Agents:
    """Return Agents: either live OpenAI-backed agents or smart demo agents."""
    if agent_mode() == "live":
        try:
            # If OpenAI agents pipeline is configured
            from app.agents.pipeline import build_agents
            return build_agents()
        except Exception:
            pass
    return build_demo_agents(project_id, text)


# ---------- Workflow Execution Handlers ----------
def submit_update(project_id: int, text: str, role: str = "project_manager",
                  session_id: Optional[str] = None,
                  agents: Optional[Agents] = None) -> dict[str, Any]:
    """Execute WF-01 project update pipeline."""
    ensure_database()
    conn = get_connection()
    try:
        ctx = ToolContext(conn=conn, role=_role(role))
        active_agents = agents or get_agents(project_id=project_id, text=text)
        res = asyncio.run(run_project_update(ctx, int(project_id), text, active_agents))
        return view_of_run(res)
    finally:
        conn.close()


def view_of_run(res: RunResult) -> dict[str, Any]:
    """Format RunResult into a rich UI dictionary."""
    badge, headline = STATUS_TEXT.get(res.status, ("?", "Unknown status"))

    chain_data = {}
    if res.update:
        chain_data["1_extracted_update"] = res.update.model_dump(mode="json")
    if res.risk:
        chain_data["2_risk_report"] = res.risk.model_dump(mode="json")
    if res.route:
        chain_data["3_router"] = res.route.model_dump(mode="json")
    if res.findings:
        chain_data["4_findings"] = res.findings.model_dump(mode="json")

    # Format actions table: Action, Task, Outcome, Detail, Approval id
    actions_table = []
    for a in res.actions:
        actions_table.append([
            a.get("action", ""),
            str(a.get("task", "")),
            a.get("outcome", ""),
            a.get("detail", ""),
            str(a.get("approval_id", ""))
        ])

    return {
        "status": res.status,
        "badge": badge,
        "headline": headline,
        "message": res.message,
        "report": res.report.model_dump(mode="json") if res.report else None,
        "violations": res.violations,
        "chain": chain_data,
        "actions": actions_table,
        "approval_ids": res.approval_ids,
        "revisions": res.revisions,
        "steps": res.steps,
    }


def report_markdown(report: Optional[dict]) -> str:
    """Render a StatusReport dictionary into clean Markdown."""
    if not report:
        return "_No report generated._"

    health = report.get("overall_health", "unknown").replace("_", " ").upper()
    health_badge = {
        "ON TRACK": "🟢 **ON TRACK**",
        "AT RISK": "🟡 **AT RISK**",
        "OFF TRACK": "🔴 **OFF TRACK**"
    }.get(health, health)

    lines = [
        f"### Overall Health: {health_badge}\n",
        f"**Summary:** {report.get('summary', '')}\n"
    ]

    blockers = report.get("blockers", [])
    if blockers:
        lines.append("\n**Blockers:**")
        for b in blockers:
            lines.append(f"- 🛑 {b}")

    risks = report.get("risks", [])
    if risks:
        lines.append("\n**Risks:**")
        for r in risks:
            lines.append(f"- ⚠️ {r}")

    actions = report.get("recommended_actions", [])
    if actions:
        lines.append("\n**Recommended Actions (Proposals, not yet executed):**")
        for a in actions:
            tid = f" (Task #{a['task_id']})" if a.get("task_id") else ""
            lines.append(f"- 📋 {a.get('action', '')}{tid}")

    evidence = report.get("evidence_task_ids", [])
    if evidence:
        lines.append(f"\n_Evidence supported by tasks: {', '.join(map(str, evidence))}_")

    return "\n".join(lines)



# ---------- Approvals & Audit Data ----------
def pending_approvals(conn: Optional[Any] = None) -> list[list]:
    """List pending approvals formatted as rows for a Gradio Dataframe."""
    close_when_done = False
    if conn is None:
        ensure_database()
        conn = get_connection()
        close_when_done = True
    try:
        ctx = ToolContext(conn=conn)
        pending = approvals_mod.list_pending(ctx)
        rows = []
        for a in pending:
            rows.append([a["id"], a["type"], a["action"], a["reason"], a["created_at"]])
        return rows
    finally:
        if close_when_done:
            conn.close()


def decide_approval(approval_id: int, approve: bool, decided_by: str = "project.manager",
                    role: str = "project_manager", conn: Optional[Any] = None) -> str:
    """Decide an approval item (approve or reject)."""
    close_when_done = False
    if conn is None:
        ensure_database()
        conn = get_connection()
        close_when_done = True
    try:
        ctx = ToolContext(conn=conn, role=_role(role))
        res = approvals_mod.decide(ctx, int(approval_id), approve, decided_by.strip() or "project.manager")
        if "error" in res:
            return f"[ERROR] {res['error']} {res.get('detail', '')}"
        status = res.get("status")
        executed = res.get("result")
        action_note = " The held action was executed in the database." if executed else ""
        return f"[OK] Approval #{approval_id} was successfully marked as '{status}'.{action_note}"
    finally:
        if close_when_done:
            conn.close()


def recent_audit(limit: int = 50, conn: Optional[Any] = None) -> list[list]:
    """Fetch recent audit log entries."""
    close_when_done = False
    if conn is None:
        ensure_database()
        conn = get_connection()
        close_when_done = True
    try:
        rows = conn.execute(
            "SELECT id, run_id, step, tool, outcome, detail, timestamp FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [list(r) for r in rows]
    finally:
        if close_when_done:
            conn.close()


def get_tasks_table(project_id: Optional[int] = None) -> list[list]:
    """Fetch tasks for database inspector."""
    ensure_database()
    conn = get_connection()
    try:
        query = "SELECT t.id, t.project_id, t.title, m.name as owner, t.status, t.priority, t.due_date, t.blocked_by FROM tasks t LEFT JOIN team_members m ON t.owner_id = m.id"
        args = []
        if project_id:
            query += " WHERE t.project_id = ?"
            args.append(project_id)
        query += " ORDER BY t.id"
        rows = conn.execute(query, args).fetchall()
        return [list(r) for r in rows]
    finally:
        conn.close()


def get_team_table() -> list[list]:
    """Fetch team members for database inspector."""
    ensure_database()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, role, capacity_hours, assigned_hours, available FROM team_members ORDER BY id"
        ).fetchall()
        return [[r[0], r[1], r[2], r[3], r[4], "Yes" if r[5] else "No (On Leave)"] for r in rows]
    finally:
        conn.close()
