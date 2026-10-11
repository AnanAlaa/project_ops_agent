import unittest
from datetime import date, timedelta

from app import approvals, tools
from app.context import ToolContext
from app.db import reset_database

TODAY = date(2026, 10, 11)
_OPEN = []


def tearDownModule():
    for c in _OPEN:
        c.close()


def make_ctx(**kw):
    conn = reset_database(":memory:", TODAY)
    _OPEN.append(conn)
    return ToolContext(conn=conn, today=TODAY, actions_enabled=kw.pop("actions_enabled", True), **kw)


def task(ctx, tid):
    return dict(ctx.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def audit_outcomes(ctx):
    return [r["outcome"] for r in ctx.conn.execute("SELECT outcome FROM audit_log ORDER BY id")]


class ReadTools(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_get_project_tasks_all_and_filtered(self):
        self.assertEqual(len(tools.get_project_tasks(self.ctx, 1)["tasks"]), 10)
        blocked = tools.get_project_tasks(self.ctx, 1, "blocked")["tasks"]
        self.assertEqual(sorted(t["id"] for t in blocked), [2, 9])

    def test_overdue_flag(self):
        tasks = {t["id"]: t for t in tools.get_project_tasks(self.ctx, 1)["tasks"]}
        self.assertTrue(tasks[5]["overdue"] and tasks[6]["overdue"])
        self.assertFalse(tasks[1]["overdue"])          # done, so never overdue

    def test_unknown_project_and_bad_status(self):
        self.assertIn("error", tools.get_project_tasks(self.ctx, 99))
        self.assertIn("error", tools.get_project_tasks(self.ctx, 1, "finished"))

    def test_get_task(self):
        t = tools.get_task(self.ctx, 2)["task"]
        self.assertEqual((t["owner"], t["status"]), ("Ahmed", "blocked"))
        self.assertIn("error", tools.get_task(self.ctx, 999))
        self.assertIn("error", tools.get_task(self.ctx, "abc"))

    def test_team_members_flags(self):
        m = {x["id"]: x for x in tools.get_team_members(self.ctx, 1)["members"]}
        self.assertFalse(m[5]["available"])
        self.assertTrue(m[3]["overloaded"])

    def test_deadline(self):
        self.assertEqual(tools.get_project_deadline(self.ctx, 1)["days_remaining"], 14)
        self.assertIn("error", tools.get_project_deadline(self.ctx, 42))

    def test_reads_are_audited(self):
        tools.get_task(self.ctx, 1)
        self.assertIn("ok", audit_outcomes(self.ctx))


class WriteGates(unittest.TestCase):
    def test_kill_switch_blocks_all_writes(self):
        ctx = make_ctx(actions_enabled=False)
        for r in (tools.update_task(ctx, 3, status="done"), tools.assign_task(ctx, 8, 4),
                  tools.create_task(ctx, 1, "New task"), tools.create_project_comment(ctx, 1, "hi")):
            self.assertTrue(r.get("blocked"))
        self.assertEqual(task(ctx, 3)["status"], "in_progress")

    def test_viewer_cannot_write(self):
        ctx = make_ctx(role="viewer")
        self.assertTrue(tools.update_task(ctx, 3, status="done").get("blocked"))
        self.assertEqual(task(ctx, 3)["status"], "in_progress")

    def test_no_delete_tool_exists(self):
        self.assertFalse([n for n in dir(tools) if "delete" in n.lower() or "remove" in n.lower()])


class UpdateTask(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_in_progress_to_done_allowed(self):
        r = tools.update_task(self.ctx, 3, status="done")
        self.assertEqual(r["task"]["status"], "done")

    def test_todo_to_done_needs_approval_and_changes_nothing(self):
        r = tools.update_task(self.ctx, 7, status="done")
        self.assertIn("pending_approval", r)
        self.assertEqual(task(self.ctx, 7)["status"], "todo")

    def test_blocked_to_done_needs_approval(self):
        self.assertIn("pending_approval", tools.update_task(self.ctx, 2, status="done"))

    def test_priority_to_critical_needs_approval(self):
        self.assertIn("pending_approval", tools.update_task(self.ctx, 1, priority="critical"))

    def test_priority_low_to_high_allowed(self):
        self.assertEqual(tools.update_task(self.ctx, 8, priority="high")["task"]["priority"], "high")

    def test_due_date_after_project_deadline_blocked(self):
        late = (TODAY + timedelta(days=20)).isoformat()
        r = tools.update_task(self.ctx, 3, due_date=late)
        self.assertTrue(r.get("blocked"))
        self.assertEqual(task(self.ctx, 3)["due_date"], (TODAY + timedelta(days=3)).isoformat())

    def test_due_date_within_deadline_allowed(self):
        ok = (TODAY + timedelta(days=9)).isoformat()
        self.assertEqual(tools.update_task(self.ctx, 3, due_date=ok)["task"]["due_date"], ok)

    def test_validation_errors(self):
        for kw in ({"status": "finished"}, {"priority": "urgent"}, {"due_date": "tomorrow"}, {}):
            self.assertIn("error", tools.update_task(self.ctx, 3, **kw))
        self.assertIn("error", tools.update_task(self.ctx, 999, status="done"))

    def test_leaving_blocked_clears_reason(self):
        tools.update_task(self.ctx, 9, status="in_progress")
        self.assertIsNone(task(self.ctx, 9)["blocked_by"])


class AssignTask(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_unavailable_member_blocked(self):
        r = tools.assign_task(self.ctx, 8, 5)
        self.assertTrue(r.get("blocked"))
        self.assertEqual(task(self.ctx, 8)["owner_id"], 1)

    def test_overloaded_member_needs_approval(self):
        self.assertIn("pending_approval", tools.assign_task(self.ctx, 8, 3))

    def test_critical_task_reassign_needs_approval(self):
        self.assertIn("pending_approval", tools.assign_task(self.ctx, 2, 4))

    def test_normal_assignment_allowed(self):
        self.assertEqual(tools.assign_task(self.ctx, 8, 4)["task"]["owner_id"], 4)

    def test_same_owner_blocked(self):
        self.assertTrue(tools.assign_task(self.ctx, 7, 4).get("blocked"))

    def test_bulk_reassignment_third_needs_approval(self):
        self.assertIn("task", tools.assign_task(self.ctx, 8, 4))
        self.assertIn("task", tools.assign_task(self.ctx, 12, 4))
        self.assertIn("pending_approval", tools.assign_task(self.ctx, 4, 4))

    def test_unknown_ids(self):
        self.assertIn("error", tools.assign_task(self.ctx, 999, 4))
        self.assertIn("error", tools.assign_task(self.ctx, 8, 999))


class CreateTaskAndComment(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_create_ok(self):
        r = tools.create_task(self.ctx, 1, "Write release notes", due_date=(TODAY + timedelta(days=9)).isoformat())
        self.assertEqual(task(self.ctx, r["task_id"])["status"], "todo")

    def test_create_validation(self):
        self.assertIn("error", tools.create_task(self.ctx, 1, "x"))
        self.assertIn("error", tools.create_task(self.ctx, 99, "Valid title"))
        self.assertIn("error", tools.create_task(self.ctx, 1, "Valid title", priority="urgent"))
        self.assertIn("error", tools.create_task(self.ctx, 1, "Valid title", due_date="soon"))

    def test_create_after_deadline_blocked(self):
        late = (TODAY + timedelta(days=30)).isoformat()
        self.assertTrue(tools.create_task(self.ctx, 1, "Late task", due_date=late).get("blocked"))

    def test_create_for_unavailable_owner_blocked(self):
        self.assertTrue(tools.create_task(self.ctx, 1, "Design task", owner_id=5).get("blocked"))

    def test_create_limit_per_run(self):
        for i in range(5):
            self.assertIn("task_id", tools.create_task(self.ctx, 1, f"Task number {i}"))
        self.assertTrue(tools.create_task(self.ctx, 1, "One too many").get("blocked"))

    def test_comment(self):
        self.assertIn("comment_id", tools.create_project_comment(self.ctx, 1, "Status looks fine", task_id=3))
        self.assertIn("error", tools.create_project_comment(self.ctx, 1, ""))
        self.assertIn("error", tools.create_project_comment(self.ctx, 1, "x" * 501))
        self.assertIn("error", tools.create_project_comment(self.ctx, 1, "wrong project", task_id=11))


class ApprovalFlow(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()

    def test_approve_executes_change(self):
        pid = tools.update_task(self.ctx, 7, status="done")["pending_approval"]
        self.assertEqual(len(approvals.list_pending(self.ctx)), 1)
        r = approvals.decide(self.ctx, pid, True, "Layla")
        self.assertEqual(r["status"], "approved")
        self.assertEqual(task(self.ctx, 7)["status"], "done")
        self.assertEqual(approvals.list_pending(self.ctx), [])

    def test_reject_changes_nothing(self):
        pid = tools.assign_task(self.ctx, 8, 3)["pending_approval"]
        self.assertEqual(approvals.decide(self.ctx, pid, False, "Layla")["status"], "rejected")
        self.assertEqual(task(self.ctx, 8)["owner_id"], 1)

    def test_deciding_twice_is_refused(self):
        pid = tools.update_task(self.ctx, 7, status="done")["pending_approval"]
        approvals.decide(self.ctx, pid, True, "Layla")
        self.assertIn("error", approvals.decide(self.ctx, pid, True, "Layla"))

    def test_viewer_cannot_decide(self):
        pid = tools.update_task(self.ctx, 7, status="done")["pending_approval"]
        viewer = ToolContext(conn=self.ctx.conn, today=TODAY, role="viewer")
        self.assertIn("error", approvals.decide(viewer, pid, True, "Someone"))
        self.assertEqual(task(self.ctx, 7)["status"], "todo")

    def test_approval_never_overrides_a_hard_block(self):
        pid = tools.assign_task(self.ctx, 8, 3)["pending_approval"]
        self.ctx.conn.execute("UPDATE team_members SET available = 0 WHERE id = 3")
        r = approvals.decide(self.ctx, pid, True, "Layla")
        self.assertIn("error", r)
        self.assertEqual(task(self.ctx, 8)["owner_id"], 1)

    def test_every_step_is_audited(self):
        pid = tools.update_task(self.ctx, 7, status="done")["pending_approval"]
        approvals.decide(self.ctx, pid, True, "Layla")
        out = audit_outcomes(self.ctx)
        self.assertIn("pending_approval", out)
        self.assertIn("approved", out)


if __name__ == "__main__":
    unittest.main()
