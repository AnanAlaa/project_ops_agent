"""Human-in-the-loop approval queue (workflow WF-03)."""
from __future__ import annotations

import json
from typing import Optional

from app.context import ToolContext


def create_approval(ctx: ToolContext, approval_type: str, action: str, payload: dict, reason: str) -> int:
    cur = ctx.conn.execute(
        "INSERT INTO approvals (run_id, type, action, payload, reason) VALUES (?,?,?,?,?)",
        (ctx.run_id, approval_type, action, json.dumps(payload, default=str), reason),
    )
    ctx.conn.commit()
    return cur.lastrowid


def list_pending(ctx: ToolContext) -> list[dict]:
    rows = ctx.conn.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def decide(ctx: ToolContext, approval_id: int, approve: bool, decided_by: str) -> dict:
    """Approve or reject. Only a project manager may decide. Deciding twice has no effect (idempotent)."""
    from app.tools import audit, execute_approved

    if ctx.role != "project_manager":
        audit(ctx, "approval", "blocked", f"role '{ctx.role}' cannot decide", step="approval")
        return {"error": "Only a project manager can approve or reject."}
    row = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        return {"error": f"Approval {approval_id} not found."}
    if row["status"] != "pending":
        return {"error": f"Approval {approval_id} was already {row['status']}."}

    result: Optional[dict] = None
    new_status = "approved" if approve else "rejected"
    if approve:
        payload = json.loads(row["payload"] or "{}")
        if payload.get("tool"):
            result = execute_approved(ctx, payload["tool"], payload.get("args", {}))
            if "error" in result or result.get("blocked"):
                audit(ctx, "approval", "failed", f"approval {approval_id}: {result}", step="approval")
                return {"error": "Approval could not be executed.", "detail": result}
    ctx.conn.execute(
        "UPDATE approvals SET status = ?, decided_by = ?, decided_at = datetime('now') WHERE id = ?",
        (new_status, decided_by, approval_id),
    )
    ctx.conn.commit()
    audit(ctx, "approval", new_status, f"approval {approval_id} by {decided_by}", step="approval")
    return {"status": new_status, "result": result}
