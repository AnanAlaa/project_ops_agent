"""SQLite schema and synthetic seed data (synthetic only: no real people or customer data)."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    deadline TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_members (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    capacity_hours INTEGER NOT NULL,       -- hours per week
    assigned_hours INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 1   -- 0 = on leave
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    owner_id INTEGER REFERENCES team_members(id),
    status TEXT NOT NULL CHECK (status IN ('todo','in_progress','blocked','done')),
    priority TEXT NOT NULL CHECK (priority IN ('low','medium','high','critical')),
    due_date TEXT,
    blocked_by TEXT                         -- free text reason, or NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    task_id INTEGER REFERENCES tasks(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    tool TEXT,
    outcome TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or os.getenv("DB_PATH", "data/project_ops.db")
    if path != ":memory:":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seed(conn: sqlite3.Connection, today: Optional[date] = None) -> None:
    """Reset and fill with demo data. Dates are relative to `today`, so overdue stays overdue."""
    today = today or date.today()

    def d(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    for table in ("audit_log", "approvals", "comments", "tasks", "team_members", "projects"):
        conn.execute(f"DELETE FROM {table}")

    conn.executemany(
        "INSERT INTO projects (id, name, deadline) VALUES (?,?,?)",
        [(1, "Mobile App Launch", d(14)), (2, "Website Redesign", d(45))],
    )
    # id, name, role, capacity, assigned, available
    conn.executemany(
        "INSERT INTO team_members (id, name, role, capacity_hours, assigned_hours, available) VALUES (?,?,?,?,?,?)",
        [
            (1, "Layla", "Project Manager", 30, 20, 1),
            (2, "Sara", "Frontend Developer", 40, 32, 1),
            (3, "Ahmed", "Backend Developer", 40, 52, 1),   # overloaded
            (4, "Mona", "QA Engineer", 40, 18, 1),
            (5, "Omar", "Designer", 40, 10, 0),             # on leave
        ],
    )
    # id, project, title, owner, status, priority, due, blocked_by
    conn.executemany(
        "INSERT INTO tasks (id, project_id, title, owner_id, status, priority, due_date, blocked_by) VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 1, "Login page", 2, "done", "high", d(-6), None),
            (2, 1, "Payment API integration", 3, "blocked", "critical", d(2), "Waiting for payment provider sandbox keys"),
            (3, 1, "User profile screen", 2, "in_progress", "medium", d(3), None),
            (4, 1, "Push notifications", 3, "todo", "medium", d(7), None),
            (5, 1, "Onboarding screens design", 5, "in_progress", "high", d(-3), None),   # overdue, owner on leave
            (6, 1, "Backend performance tuning", 3, "in_progress", "high", d(-2), None),   # overdue
            (7, 1, "Regression test plan", 4, "todo", "medium", d(5), None),
            (8, 1, "App store listing", 1, "todo", "low", d(10), None),
            (9, 1, "Crash reporting setup", 3, "blocked", "medium", d(6), "Needs decision on vendor"),
            (10, 1, "QA of login and profile", 4, "in_progress", "medium", d(8), None),
            (11, 2, "Homepage wireframes", 5, "in_progress", "medium", d(12), None),
            (12, 2, "Content migration plan", 1, "todo", "low", d(20), None),
            (13, 2, "Accessibility audit", 4, "todo", "medium", d(25), None),
            (14, 2, "Navigation redesign", 2, "done", "medium", d(-10), None),
        ],
    )
    conn.executemany(
        "INSERT INTO comments (project_id, task_id, text) VALUES (?,?,?)",
        [(1, 2, "Provider promised keys by end of week."), (1, 5, "Designer is on leave until next month.")],
    )
    conn.commit()


def reset_database(path: Optional[str] = None, today: Optional[date] = None) -> sqlite3.Connection:
    conn = get_connection(path)
    init_db(conn)
    seed(conn, today)
    return conn
