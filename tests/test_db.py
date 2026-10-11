import unittest
from datetime import date

from app.db import reset_database

TODAY = date(2026, 10, 11)
_OPEN = []


def tearDownModule():
    for c in _OPEN:
        c.close()


class SeedData(unittest.TestCase):
    def setUp(self):
        self.conn = reset_database(":memory:", TODAY)
        _OPEN.append(self.conn)

    def q(self, sql, *a):
        return [dict(r) for r in self.conn.execute(sql, a)]

    def test_counts(self):
        self.assertEqual(self.q("SELECT COUNT(*) c FROM projects")[0]["c"], 2)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM team_members")[0]["c"], 5)
        self.assertEqual(self.q("SELECT COUNT(*) c FROM tasks")[0]["c"], 14)

    def test_has_overdue_open_tasks(self):
        rows = self.q("SELECT id FROM tasks WHERE status != 'done' AND due_date < ?", TODAY.isoformat())
        self.assertGreaterEqual(len(rows), 2)

    def test_has_blocked_tasks_with_reason(self):
        rows = self.q("SELECT id FROM tasks WHERE status = 'blocked' AND blocked_by IS NOT NULL")
        self.assertGreaterEqual(len(rows), 2)

    def test_has_critical_blocker_near_deadline(self):
        rows = self.q("SELECT id FROM tasks WHERE status='blocked' AND priority='critical' AND due_date <= ?", (TODAY.replace(day=14)).isoformat())
        self.assertEqual([r["id"] for r in rows], [2])

    def test_has_unavailable_and_overloaded_members(self):
        self.assertEqual([r["id"] for r in self.q("SELECT id FROM team_members WHERE available = 0")], [5])
        self.assertEqual([r["id"] for r in self.q("SELECT id FROM team_members WHERE assigned_hours > capacity_hours")], [3])

    def test_status_constraint_enforced(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tasks SET status='finished' WHERE id=1")

    def test_reseed_resets(self):
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=2")
        from app.db import seed
        seed(self.conn, TODAY)
        self.assertEqual(self.q("SELECT status FROM tasks WHERE id=2")[0]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
