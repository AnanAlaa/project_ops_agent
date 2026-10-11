"""The 8 tools as plain Python functions (testable without the SDK).

Rules (from the brief): Python decides what is allowed. Write tools run their own authorization check,
so the model cannot bypass it. Tools never raise to the model: errors come back as {"error": ...}.
There is deliberately NO delete tool.

Every write goes through: validate -> decide (allow / needs approval / block) -> apply or queue.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from app.context import BULK_REASSIGN_LIMIT, MAX_CREATES_PER_RUN, ToolContext

STATUSES = ("todo", "in_progress", "blocked", "done")
PRIORITIES = ("low", "medium", "high", "critical")
MAX_TITLE = 200
MAX_COMMENT = 500


# ---------- small helpers ----------
@dataclass
class Decision:
    kind: str                     # "allow" | "approve" | "block"
    reason: str = ""
    approval_type: str = "significant_change"


def audit(ctx: ToolContext, tool: str, outcome: str, detail: str = "", step: str = "tool") -> None:
    ctx.conn.execute(
        "INSERT INTO audit_log (run_id, step, tool, outcome, detail) VALUES (?,?,?,?,?)",
        (ctx.run_id, step, tool, outcome, detail[:500]),
    )
    ctx.conn.commit()


def _err(ctx: ToolContext, tool: str, message: str) -> dict:
    audit(ctx, tool, "error", message)
    return {"error": message}


def _parse_date(value: Optional[str]) -> tuple[Optional[date], Optional[str]]:
    if value is None:
        return None, None
    try:
        return date.fromisoformat(value), None
    except (TypeError, ValueError):
        return None, f"Invalid date '{value}'. Use YYYY-MM-DD."


def _project(ctx: ToolContext, project_id) -> Optional[sqlite3.Row]:
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        return None
    return ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def _task_dict(ctx: ToolContext, r: sqlite3.Row) -> dict:
    overdue = bool(r["due_date"] and r["status"] != "done" and r["due_date"] < ctx.today.isoformat())
    owner = None
    if r["owner_id"] is not None:
        o = ctx.conn.execute("SELECT name FROM team_members WHERE id = ?", (r["owner_id"],)).fetchone()
        owner = o["name"] if o else None
    return {
        "id": r["id"], "project_id": r["project_id"], "title": r["title"],
        "owner_id": r["owner_id"], "owner": owner, "status": r["status"], "priority": r["priority"],
        "due_date": r["due_date"], "blocked_by": r["blocked_by"], "overdue": overdue,
    }


def _write_gate(ctx: ToolContext, tool: str) -> Optional[dict]:
    """Checks that apply to every write tool: kill switch and role."""
    if not ctx.actions_enabled:
        audit(ctx, tool, "blocked", "actions disabled (kill switch)")
        return {"blocked": True, "reason": "Actions are disabled. The system is read-only right now."}
    if ctx.role != "project_manager":
        audit(ctx, tool, "blocked", f"role '{ctx.role}' cannot write")
        return {"blocked": True, "reason": "Your role is read-only. Only a project manager can change tasks."}
    return None


# ---------- read tools ----------
def get_project_tasks(ctx: ToolContext, project_id: int, status: Optional[str] = None) -> dict:
    try:
        if _project(ctx, project_id) is None:
            return _err(ctx, "get_project_tasks", f"Project {project_id} not found.")
        if status is not None and status not in STATUSES:
            return _err(ctx, "get_project_tasks", f"Invalid status '{status}'. Use one of {list(STATUSES)}.")
        sql, args = "SELECT * FROM tasks WHERE project_id = ?", [project_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        rows = ctx.conn.execute(sql + " ORDER BY id", args).fetchall()
        audit(ctx, "get_project_tasks", "ok", f"project {project_id}, {len(rows)} tasks")
        return {"tasks": [_task_dict(ctx, r) for r in rows]}
    except sqlite3.Error as e:
        return _err(ctx, "get_project_tasks", f"Database error: {e}")


def get_task(ctx: ToolContext, task_id: int) -> dict:
    try:
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            return _err(ctx, "get_task", "task_id must be an integer.")
        r = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None:
            return _err(ctx, "get_task", f"Task {task_id} not found.")
        audit(ctx, "get_task", "ok", f"task {task_id}")
        return {"task": _task_dict(ctx, r)}
    except sqlite3.Error as e:
        return _err(ctx, "get_task", f"Database error: {e}")


def get_team_members(ctx: ToolContext, project_id: int) -> dict:
    try:
        if _project(ctx, project_id) is None:
            return _err(ctx, "get_team_members", f"Project {project_id} not found.")
        rows = ctx.conn.execute("SELECT * FROM team_members ORDER BY id").fetchall()
        members = []
        for m in rows:
            open_tasks = ctx.conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE owner_id = ? AND project_id = ? AND status != 'done'",
                (m["id"], project_id),
            ).fetchone()["c"]
            members.append({
                "id": m["id"], "name": m["name"], "role": m["role"],
                "capacity_hours": m["capacity_hours"], "assigned_hours": m["assigned_hours"],
                "available": bool(m["available"]),
                "overloaded": m["assigned_hours"] > m["capacity_hours"],
                "open_tasks_in_project": open_tasks,
            })
        audit(ctx, "get_team_members", "ok", f"project {project_id}")
        return {"members": members}
    except sqlite3.Error as e:
        return _err(ctx, "get_team_members", f"Database error: {e}")


def get_project_deadline(ctx: ToolContext, project_id: int) -> dict:
    try:
        p = _project(ctx, project_id)
        if p is None:
            return _err(ctx, "get_project_deadline", f"Project {project_id} not found.")
        deadline = date.fromisoformat(p["deadline"])
        audit(ctx, "get_project_deadline", "ok", f"project {project_id}")
        return {"deadline": p["deadline"], "days_remaining": (deadline - ctx.today).days}
    except sqlite3.Error as e:
        return _err(ctx, "get_project_deadline", f"Database error: {e}")


# ---------- decisions (pure rules, used by the tools and re-used when an approval is executed) ----------
def decide_update(ctx: ToolContext, task: sqlite3.Row, status: Optional[str],
                  due: Optional[date], priority: Optional[str]) -> Decision:
    project = _project(ctx, task["project_id"])
    if due is not None and project is not None and due > date.fromisoformat(project["deadline"]):
        return Decision("block", f"Due date {due} is after the project deadline {project['deadline']}. "
                                 "Moving it needs a decision about the project deadline, not a task edit.")
    if status == "done" and task["status"] in ("todo", "blocked"):
        return Decision("approve", f"Marking a '{task['status']}' task as done needs human confirmation.")
    if priority is not None and priority != task["priority"] and "critical" in (priority, task["priority"]):
        return Decision("approve", "Changing priority to or from critical has significant impact.")
    return Decision("allow")


def decide_assign(ctx: ToolContext, task: sqlite3.Row, member: sqlite3.Row) -> Decision:
    if not member["available"]:
        return Decision("block", f"{member['name']} is unavailable (on leave).")
    if task["owner_id"] == member["id"]:
        return Decision("block", f"Task is already assigned to {member['name']}.")
    if member["assigned_hours"] > member["capacity_hours"]:
        return Decision("approve", f"{member['name']} is already over capacity.")
    if task["priority"] == "critical":
        return Decision("approve", "Reassigning a critical task has significant impact.")
    if ctx.reassign_count + 1 >= BULK_REASSIGN_LIMIT:
        return Decision("approve", "Bulk reassignment: several tasks reassigned in one request.")
    return Decision("allow")


def decide_create(ctx: ToolContext, project: sqlite3.Row, owner: Optional[sqlite3.Row],
                  due: Optional[date]) -> Decision:
    if ctx.created_count >= MAX_CREATES_PER_RUN:
        return Decision("block", f"Limit of {MAX_CREATES_PER_RUN} new tasks per request reached.")
    if due is not None and due > date.fromisoformat(project["deadline"]):
        return Decision("block", f"Due date {due} is after the project deadline {project['deadline']}.")
    if owner is not None and not owner["available"]:
        return Decision("block", f"{owner['name']} is unavailable (on leave).")
    return Decision("allow")


# ---------- appliers (the only code that changes tasks) ----------
def _apply_update(ctx, task_id, status, due_date, priority) -> dict:
    sets, args = [], []
    if status is not None:
        sets.append("status = ?"); args.append(status)
        if status != "blocked":
            sets.append("blocked_by = NULL")
    if due_date is not None:
        sets.append("due_date = ?"); args.append(due_date)
    if priority is not None:
        sets.append("priority = ?"); args.append(priority)
    ctx.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", args + [task_id])
    ctx.conn.commit()
    return _task_dict(ctx, ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def _apply_assign(ctx, task_id, member_id) -> dict:
    ctx.conn.execute("UPDATE tasks SET owner_id = ? WHERE id = ?", (member_id, task_id))
    ctx.conn.commit()
    return _task_dict(ctx, ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def _apply_create(ctx, project_id, title, due_date, priority, owner_id) -> int:
    cur = ctx.conn.execute(
        "INSERT INTO tasks (project_id, title, owner_id, status, priority, due_date) VALUES (?,?,?,?,?,?)",
        (project_id, title, owner_id, "todo", priority, due_date),
    )
    ctx.conn.commit()
    return cur.lastrowid


def _queue(ctx: ToolContext, tool: str, args: dict, decision: Decision) -> dict:
    from app.approvals import create_approval
    approval_id = create_approval(
        ctx, decision.approval_type, f"{tool} {json.dumps(args, default=str)}",
        {"tool": tool, "args": args}, decision.reason,
    )
    audit(ctx, tool, "pending_approval", f"approval {approval_id}: {decision.reason}")
    return {"pending_approval": approval_id, "reason": decision.reason,
            "message": "This change was NOT made. It is waiting for human approval."}


# ---------- write tools ----------
def update_task(ctx: ToolContext, task_id: int, status: Optional[str] = None,
                due_date: Optional[str] = None, priority: Optional[str] = None) -> dict:
    try:
        if (gate := _write_gate(ctx, "update_task")):
            return gate
        if status is None and due_date is None and priority is None:
            return _err(ctx, "update_task", "Nothing to change. Give status, due_date or priority.")
        if status is not None and status not in STATUSES:
            return _err(ctx, "update_task", f"Invalid status '{status}'. Use one of {list(STATUSES)}.")
        if priority is not None and priority not in PRIORITIES:
            return _err(ctx, "update_task", f"Invalid priority '{priority}'. Use one of {list(PRIORITIES)}.")
        due, msg = _parse_date(due_date)
        if msg:
            return _err(ctx, "update_task", msg)
        task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() if isinstance(task_id, int) else None
        if task is None:
            return _err(ctx, "update_task", f"Task {task_id} not found.")
        d = decide_update(ctx, task, status, due, priority)
        args = {"task_id": task_id, "status": status, "due_date": due_date, "priority": priority}
        if d.kind == "block":
            audit(ctx, "update_task", "blocked", d.reason)
            return {"blocked": True, "reason": d.reason}
        if d.kind == "approve":
            return _queue(ctx, "update_task", args, d)
        result = _apply_update(ctx, task_id, status, due_date, priority)
        audit(ctx, "update_task", "ok", f"task {task_id}")
        return {"task": result}
    except sqlite3.Error as e:
        return _err(ctx, "update_task", f"Database error: {e}")


def assign_task(ctx: ToolContext, task_id: int, member_id: int) -> dict:
    try:
        if (gate := _write_gate(ctx, "assign_task")):
            return gate
        task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() if isinstance(task_id, int) else None
        if task is None:
            return _err(ctx, "assign_task", f"Task {task_id} not found.")
        member = ctx.conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone() if isinstance(member_id, int) else None
        if member is None:
            return _err(ctx, "assign_task", f"Team member {member_id} not found.")
        d = decide_assign(ctx, task, member)
        args = {"task_id": task_id, "member_id": member_id}
        if d.kind == "block":
            audit(ctx, "assign_task", "blocked", d.reason)
            return {"blocked": True, "reason": d.reason}
        ctx.reassign_count += 1
        if d.kind == "approve":
            return _queue(ctx, "assign_task", args, d)
        result = _apply_assign(ctx, task_id, member_id)
        audit(ctx, "assign_task", "ok", f"task {task_id} -> member {member_id}")
        return {"task": result}
    except sqlite3.Error as e:
        return _err(ctx, "assign_task", f"Database error: {e}")


def create_task(ctx: ToolContext, project_id: int, title: str, due_date: Optional[str] = None,
                priority: str = "medium", owner_id: Optional[int] = None) -> dict:
    try:
        if (gate := _write_gate(ctx, "create_task")):
            return gate
        project = _project(ctx, project_id)
        if project is None:
            return _err(ctx, "create_task", f"Project {project_id} not found.")
        title = (title or "").strip()
        if not 3 <= len(title) <= MAX_TITLE:
            return _err(ctx, "create_task", f"Title must be 3 to {MAX_TITLE} characters.")
        if priority not in PRIORITIES:
            return _err(ctx, "create_task", f"Invalid priority '{priority}'. Use one of {list(PRIORITIES)}.")
        due, msg = _parse_date(due_date)
        if msg:
            return _err(ctx, "create_task", msg)
        owner = None
        if owner_id is not None:
            owner = ctx.conn.execute("SELECT * FROM team_members WHERE id = ?", (owner_id,)).fetchone() if isinstance(owner_id, int) else None
            if owner is None:
                return _err(ctx, "create_task", f"Team member {owner_id} not found.")
        d = decide_create(ctx, project, owner, due)
        if d.kind == "block":
            audit(ctx, "create_task", "blocked", d.reason)
            return {"blocked": True, "reason": d.reason}
        new_id = _apply_create(ctx, project_id, title, due_date, priority, owner_id)
        ctx.created_count += 1
        audit(ctx, "create_task", "ok", f"task {new_id}")
        return {"task_id": new_id}
    except sqlite3.Error as e:
        return _err(ctx, "create_task", f"Database error: {e}")


def create_project_comment(ctx: ToolContext, project_id: int, text: str, task_id: Optional[int] = None) -> dict:
    try:
        if (gate := _write_gate(ctx, "create_project_comment")):
            return gate
        if _project(ctx, project_id) is None:
            return _err(ctx, "create_project_comment", f"Project {project_id} not found.")
        text = (text or "").strip()
        if not 1 <= len(text) <= MAX_COMMENT:
            return _err(ctx, "create_project_comment", f"Comment must be 1 to {MAX_COMMENT} characters.")
        if task_id is not None:
            t = ctx.conn.execute("SELECT project_id FROM tasks WHERE id = ?", (task_id,)).fetchone() if isinstance(task_id, int) else None
            if t is None or t["project_id"] != project_id:
                return _err(ctx, "create_project_comment", f"Task {task_id} not found in project {project_id}.")
        cur = ctx.conn.execute(
            "INSERT INTO comments (project_id, task_id, text) VALUES (?,?,?)", (project_id, task_id, text))
        ctx.conn.commit()
        audit(ctx, "create_project_comment", "ok", f"comment {cur.lastrowid}")
        return {"comment_id": cur.lastrowid}
    except sqlite3.Error as e:
        return _err(ctx, "create_project_comment", f"Database error: {e}")


# ---------- executing an approved change ----------
def execute_approved(ctx: ToolContext, tool: str, args: dict) -> dict:
    """Run a change a human approved. Hard blocks are re-checked: approval never overrides them."""
    try:
        if tool == "update_task":
            task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (args["task_id"],)).fetchone()
            if task is None:
                return {"error": "Task no longer exists."}
            due, _ = _parse_date(args.get("due_date"))
            d = decide_update(ctx, task, args.get("status"), due, args.get("priority"))
            if d.kind == "block":
                return {"blocked": True, "reason": d.reason}
            return {"task": _apply_update(ctx, args["task_id"], args.get("status"), args.get("due_date"), args.get("priority"))}
        if tool == "assign_task":
            task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?", (args["task_id"],)).fetchone()
            member = ctx.conn.execute("SELECT * FROM team_members WHERE id = ?", (args["member_id"],)).fetchone()
            if task is None or member is None:
                return {"error": "Task or team member no longer exists."}
            if not member["available"]:
                return {"blocked": True, "reason": f"{member['name']} is unavailable (on leave)."}
            return {"task": _apply_assign(ctx, args["task_id"], args["member_id"])}
        return {"error": f"Tool '{tool}' cannot be executed from an approval."}
    except sqlite3.Error as e:
        return {"error": f"Database error: {e}"}
