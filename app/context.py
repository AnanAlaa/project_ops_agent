"""Run context passed to every tool (RunContextWrapper[ToolContext] in the Agents SDK)."""
from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date

MAX_CREATES_PER_RUN = 5       # stops a runaway agent from flooding the project with tasks
BULK_REASSIGN_LIMIT = 3       # the 3rd reassignment in one run needs human approval
ROLES = ("viewer", "project_manager")


def _actions_enabled_from_env() -> bool:
    return os.getenv("AGENT_ACTIONS_ENABLED", "true").strip().lower() not in ("false", "0", "no")


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    role: str = "project_manager"              # only a project_manager may write or approve
    actions_enabled: bool = field(default_factory=_actions_enabled_from_env)   # kill switch
    today: date = field(default_factory=date.today)
    reassign_count: int = 0
    created_count: int = 0
