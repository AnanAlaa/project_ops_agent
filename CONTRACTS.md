# Contracts (agree these first; tell the other person the same day if one changes)

## 1. Models
All in `app/models.py`. Pipeline: `ProjectUpdate` -> `RiskReport` -> (workers) `TaskFindings`, `PeopleFindings`,
`DeadlineFindings` -> `StatusReport` <-> `EvaluationResult`. Python-side: `RouteDecision`, `ApprovalRequest`.

## 2. Database
Schema and seed in `app/db.py`. Reset with `python scripts/seed_db.py`. Tables: projects, team_members, tasks,
comments, approvals, audit_log. Tasks: status in todo | in_progress | blocked | done; priority in low | medium | high | critical.

## 3. Tool signatures (to implement in `app/tools.py`, owner: Anan)
Every tool returns a JSON-serializable dict. Errors return `{"error": "<message>"}`, never raise to the model.

| Tool | Signature | Kind |
|---|---|---|
| get_project_tasks | (project_id: int, status: str \| None = None) -> {"tasks": [...]} | read |
| get_task | (task_id: int) -> {"task": {...}} or {"error": ...} | read |
| get_team_members | (project_id: int) -> {"members": [...]} | read |
| get_project_deadline | (project_id: int) -> {"deadline": "YYYY-MM-DD", "days_remaining": int} | read |
| create_task | (project_id: int, title: str, due_date: str \| None, priority: str = "medium", owner_id: int \| None = None) -> {"task_id": int} | write |
| update_task | (task_id: int, status: str \| None, due_date: str \| None, priority: str \| None) -> {"task": {...}} or {"pending_approval": id} | write, protected |
| assign_task | (task_id: int, member_id: int) -> {"task": {...}} or {"pending_approval": id} | write, protected |
| create_project_comment | (project_id: int, text: str, task_id: int \| None = None) -> {"comment_id": int} | write |

Rules: no delete tool exists. Write tools run the Python authorization check themselves. A write is only reported as
done if the tool returned success.

## 4. Python rules (implemented in `app/tools.py`, `app/approvals.py`, `app/context.py`)
Every write tool returns one of: success, `{"blocked": true, "reason"}`, or `{"pending_approval": id}`. The agent must
report "blocked" and "pending_approval" as NOT done.

| Situation | Result |
|---|---|
| Kill switch off (`AGENT_ACTIONS_ENABLED=false`) or role is `viewer` | blocked |
| Due date after the project deadline (update or create) | blocked |
| Assign to an unavailable member, or to the current owner | blocked |
| Create a task for an unavailable owner; more than 5 new tasks in one run | blocked |
| Mark a `todo` or `blocked` task as done | needs approval |
| Priority changed to or from `critical` | needs approval |
| Assign to an overloaded member; reassign a critical task; 3rd reassignment in one run | needs approval |
| Everything else valid | allowed |

Approvals: `approvals.decide(ctx, id, approve, decided_by)`. Only a `project_manager` may decide, deciding twice is
refused, and an approval never overrides a hard block (re-checked at execution time).

## 4b. Routing and guardrails (done)
- `rules.route_update(ctx, project_id, risk) -> RouteDecision`: needs_human if the RiskReport has a critical blocker or
  deadline risk, OR the database does (critical task blocked; deadline within 7 days with blocked/overdue work; deadline
  passed with open tasks). The database check means the model cannot under-report a risk.
- `guardrails.check_input(text) -> GuardrailResult`: blocks empty or too long text, prompt injection, destructive requests
  (delete/wipe tasks, projects, data), approval bypass and requests for secrets.
- `guardrails.check_output(report, ctx) -> GuardrailResult`: rejects a StatusReport that cites tasks that do not exist,
  claims a task is complete when the database says otherwise, makes delivery commitments, or mentions dates not in the data.
- SDK wrappers: `agent_guardrails.input_guard` (on the first agent) and `output_guard` (on the status writer).

Demo data: project 1 (Mobile App Launch) has a critical blocker, so every update to it is held for approval. Use project 2
(Website Redesign) for the normal happy-path scenarios.

## 4c. Workflow pipeline (done): `app/workflows/project_update.py`
`await run_project_update(ctx, project_id, text, agents) -> RunResult`. It runs the whole diagram: input guardrail ->
extract -> risk -> router -> gather -> writer/evaluator loop (max 3) with the output guardrail -> actions. Statuses:
`blocked_input`, `held_for_approval`, `completed`, `needs_review`, `rejected_output`, `error`. `RunResult.steps` is the
readable step log for the UI; `RunResult.actions` lists what each tool call REALLY returned (applied, pending_approval,
blocked, error, skipped). Retries: invalid structured output once; timeouts and 5xx 3 times with 1, 2, 4 s backoff.

Teammate: implement the 5 agent functions listed at the top of that file and pass them as `Agents(...)`. Until they
exist, `app/workflows/fakes.py` (`FakeAgents`) lets you build and test the UI with no API key.
Contract change: `ExtractedTask.status` is now optional (None = the update did not state a status), so the pipeline
never overwrites a real status with a default.

## 5. Agents (owner: teammate, `app/agents/`)
Each agent returns one of the models above through `output_type`. Config comes from `.env` (see `.env.example`).
