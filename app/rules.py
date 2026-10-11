"""Deterministic routing (workflow step 4). Python decides who must look at this, never the LLM.

The router uses two sources:
  1. the LLM's RiskReport (what the update says), and
  2. facts read straight from the database (what is actually true).
The database facts are a backstop: if the model under-reports a critical blocker, a human still reviews it.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from app.context import ToolContext
from app.models import RiskReport, RouteDecision

DEADLINE_WINDOW_DAYS = 7      # a project this close to its deadline is "at risk" if work is stuck


def db_critical_blockers(ctx: ToolContext, project_id: int) -> list[sqlite3.Row]:
    return ctx.conn.execute(
        "SELECT id, title FROM tasks WHERE project_id = ? AND status = 'blocked' AND priority = 'critical'",
        (project_id,),
    ).fetchall()


def db_deadline_risk(ctx: ToolContext, project_id: int) -> str | None:
    """Return a reason string if the project deadline is at risk, else None."""
    p = ctx.conn.execute("SELECT deadline FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        return None
    days_left = (date.fromisoformat(p["deadline"]) - ctx.today).days
    open_tasks = ctx.conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE project_id = ? AND status != 'done'", (project_id,)).fetchone()["c"]
    if days_left < 0 and open_tasks > 0:
        return f"Project deadline passed {-days_left} day(s) ago with {open_tasks} open task(s)."
    if days_left <= DEADLINE_WINDOW_DAYS:
        stuck = ctx.conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE project_id = ? AND status != 'done' "
            "AND (status = 'blocked' OR due_date < ?)", (project_id, ctx.today.isoformat())).fetchone()["c"]
        if stuck > 0:
            return f"Deadline is in {days_left} day(s) and {stuck} task(s) are blocked or overdue."
    return None


def route_update(ctx: ToolContext, project_id: int, risk: RiskReport) -> RouteDecision:
    reasons: list[str] = []
    if risk.is_critical_blocker:
        reasons.append("Risk analysis found a critical blocker.")
    if risk.deadline_at_risk:
        reasons.append("Risk analysis flagged the deadline as at risk.")
    for t in db_critical_blockers(ctx, project_id):
        reasons.append(f"Task {t['id']} ('{t['title']}') is a critical task and is blocked.")
    reason = db_deadline_risk(ctx, project_id)
    if reason:
        reasons.append(reason)
    return RouteDecision(needs_human=bool(reasons), reasons=reasons)
