"""Thin OpenAI Agents SDK wrappers around app/tools.py.

All logic lives in tools.py (unit-tested). Each wrapper only exposes the function to the model and
passes the run context (ToolContext) through RunContextWrapper.

Read tools are given to the analysis agents. Write tools are given only where the design allows them;
each one still enforces authorization itself.
"""
from __future__ import annotations

from typing import Optional

from agents import RunContextWrapper, function_tool

from app import tools
from app.context import ToolContext


@function_tool
def get_project_tasks(ctx: RunContextWrapper[ToolContext], project_id: int, status: Optional[str] = None) -> dict:
    """List the tasks of a project with status, owner, due date and an overdue flag.
    status (optional): todo, in_progress, blocked or done."""
    return tools.get_project_tasks(ctx.context, project_id, status)


@function_tool
def get_task(ctx: RunContextWrapper[ToolContext], task_id: int) -> dict:
    """Get one task with its owner, status, priority, due date and what blocks it."""
    return tools.get_task(ctx.context, task_id)


@function_tool
def get_team_members(ctx: RunContextWrapper[ToolContext], project_id: int) -> dict:
    """List team members with role, capacity, assigned hours, availability and open tasks in the project."""
    return tools.get_team_members(ctx.context, project_id)


@function_tool
def get_project_deadline(ctx: RunContextWrapper[ToolContext], project_id: int) -> dict:
    """Get the project deadline (YYYY-MM-DD) and the days remaining."""
    return tools.get_project_deadline(ctx.context, project_id)


@function_tool
def create_task(ctx: RunContextWrapper[ToolContext], project_id: int, title: str,
                due_date: Optional[str] = None, priority: str = "medium", owner_id: Optional[int] = None) -> dict:
    """Create a new task (status todo). due_date is YYYY-MM-DD. Returns the new task id, or why it was blocked.
    Never claim a task was created unless this returns a task_id."""
    return tools.create_task(ctx.context, project_id, title, due_date, priority, owner_id)


@function_tool
def update_task(ctx: RunContextWrapper[ToolContext], task_id: int, status: Optional[str] = None,
                due_date: Optional[str] = None, priority: Optional[str] = None) -> dict:
    """Change the status, due date or priority of a task. The change may be blocked or sent to human approval.
    If the result has 'pending_approval' or 'blocked', the change was NOT made: say so."""
    return tools.update_task(ctx.context, task_id, status, due_date, priority)


@function_tool
def assign_task(ctx: RunContextWrapper[ToolContext], task_id: int, member_id: int) -> dict:
    """Assign a task to a team member. May be blocked or sent to human approval.
    If the result has 'pending_approval' or 'blocked', the change was NOT made: say so."""
    return tools.assign_task(ctx.context, task_id, member_id)


@function_tool
def create_project_comment(ctx: RunContextWrapper[ToolContext], project_id: int, text: str,
                           task_id: Optional[int] = None) -> dict:
    """Add a short note (max 500 characters) to a project, or to one of its tasks."""
    return tools.create_project_comment(ctx.context, project_id, text, task_id)


READ_TOOLS = [get_project_tasks, get_task, get_team_members, get_project_deadline]
WRITE_TOOLS = [create_task, update_task, assign_task, create_project_comment]
