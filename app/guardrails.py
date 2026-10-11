"""Deterministic guardrails: pure Python, no LLM.

check_input(text)           -> run before any agent sees the text
check_output(report, ctx)   -> run on the StatusReport before it is shown or acted on

The brief: guardrails must be meaningful and tested. Output checks compare the report with the database,
so an invented task id, an invented date or a false "task is done" claim is caught.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from app.context import ToolContext
from app.models import StatusReport

MAX_INPUT_CHARS = 4000
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿"), None)


@dataclass
class GuardrailResult:
    allowed: bool
    category: Optional[str] = None
    reason: str = ""
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "category": self.category, "reason": self.reason,
                "violations": self.violations}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).lower()
    return re.sub(r"\s+", " ", text).strip()


# ---------- input guardrail ----------
_INJECTION = [
    r"\b(ignore|disregard|forget)\b (all |any |the |your |my |these |those )?(previous |prior |above |earlier |other )?"
    r"(instructions?|rules?|guidelines?|prompts?|guardrails?|restrictions?)",
    r"\b(override|bypass|disable|turn off|deactivate)\b (all |any |the |your |these )?(safety |security )?"
    r"(instructions?|rules?|guardrails?|restrictions?|checks?|authori[sz]ation|permissions?)",
    r"\byou are now\b|\bact as (an? )?(admin|administrator|root|developer|dan|unrestricted)\b|"
    r"\bdeveloper mode\b|\bjailbreak\b|\bdo anything now\b",
    r"\b(reveal|show|print|repeat|display|tell me|leak)\b (me )?(your |the )?(system |hidden |initial |secret )?"
    r"(prompt|instructions?)",
    r"</?\s*(system|assistant|instructions?)\s*>|\[\s*system\s*\]|^\s*system\s*:",
]
_APPROVAL_BYPASS = r"\b(skip|bypass|override|ignore|without)\b (the )?(human )?(approval|approvals|review|authori[sz]ation)\b|\bauto-?approve\b|\bapprove (it|this) yourself\b"
_DESTRUCTIVE = [
    r"\b(delete|drop|wipe|erase|destroy|purge|remove|truncate)\s+(?:(?:all|every|each|the|my|our|this|that|these|those|entire|whole)\s+)?"
    r"(?:\w+\s+){0,2}?(tasks?|projects?|database|tables?|comments?|team members?|everything|data)\b",
    r"\bdrop\s+table\b|;\s*(drop|delete)\b|\bor\s+1\s*=\s*1\b",
]
_SECRETS = r"\b(api[- ]?key|password|passwords|secret|token|credentials?)\b"
_SECRET_VERBS = r"\b(show|reveal|give|print|send|share|tell|display|leak|dump|list)\b"


def check_input(text: Optional[str]) -> GuardrailResult:
    if text is None or not text.strip():
        return GuardrailResult(False, "malformed", "The update is empty. Please describe what changed.")
    if len(text) > MAX_INPUT_CHARS:
        return GuardrailResult(False, "too_long", f"The update is too long (max {MAX_INPUT_CHARS} characters).")
    t = normalize(text)
    for pat in _INJECTION:
        if re.search(pat, t):
            return GuardrailResult(False, "prompt_injection",
                                   "This looks like an attempt to change the assistant's rules, so it was blocked.")
    if re.search(_APPROVAL_BYPASS, t):
        return GuardrailResult(False, "approval_bypass", "Approvals cannot be skipped or approved by the assistant.")
    for pat in _DESTRUCTIVE:
        if re.search(pat, t):
            return GuardrailResult(False, "destructive_request",
                                   "Deleting tasks, projects or data is not supported. Nothing was changed.")
    if re.search(_SECRETS, t) and re.search(_SECRET_VERBS, t):
        return GuardrailResult(False, "secrets_request", "Requests for keys, passwords or secrets are not allowed.")
    return GuardrailResult(True)


# ---------- output guardrail ----------
_COMPLETE = r"\b(complete|completed|done|finished|delivered|shipped|closed|resolved)\b"
_NOT_COMPLETE_CONTEXT = (r"\b(not|isn't|isnt|aren't|yet|pending|until|before|once|after|when|will|should|to be|"
                         r"needs?|waiting|awaiting|if|unless|incomplete|unfinished)\b")
_COMMITMENT = [
    r"\b(guarantee[sd]?|promise[sd]?|committed to|commits? to|assure[sd]?|definitely|certainly)\b",
    r"\b(will|shall) (definitely |certainly )?(be )?(deliver|delivered|ship|shipped|launch|launched|finish|finished|"
    r"complete|completed|ready|done)\b.{0,30}\b(by|on|before)\b",
]
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _all_text(report: StatusReport) -> list[str]:
    parts = [report.summary, *report.blockers, *report.risks, *[a.action for a in report.recommended_actions]]
    return [p for p in parts if p]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def check_output(report: StatusReport, ctx: ToolContext) -> GuardrailResult:
    violations: list[str] = []
    conn = ctx.conn
    tasks = {r["id"]: r for r in conn.execute("SELECT id, title, status FROM tasks")}
    known_dates = {r[0] for r in conn.execute("SELECT deadline FROM projects")}
    known_dates |= {r[0] for r in conn.execute("SELECT due_date FROM tasks WHERE due_date IS NOT NULL")}
    known_dates.add(ctx.today.isoformat())

    # 1. evidence must point at real tasks
    for tid in report.evidence_task_ids:
        if tid not in tasks:
            violations.append(f"Evidence cites task {tid}, which does not exist.")
    for a in report.recommended_actions:
        if a.task_id is not None and a.task_id not in tasks:
            violations.append(f"A recommended action refers to task {a.task_id}, which does not exist.")

    for text in _all_text(report):
        low = normalize(text)
        # 2. false completion claims, by id ("task 7 is done") and by title ("login page is done")
        for m in re.finditer(r"\btask\s*#?\s*(\d+)\b[^.!?\n]{0,40}?" + _COMPLETE, low):
            tid = int(m.group(1))
            sentence = low[max(0, m.start() - 20): m.end() + 20]
            if tid in tasks and tasks[tid]["status"] != "done" and not re.search(_NOT_COMPLETE_CONTEXT, sentence):
                violations.append(f"Claims task {tid} is complete, but its status is '{tasks[tid]['status']}'.")
        for sentence in _sentences(low):
            if not re.search(_COMPLETE, sentence) or re.search(_NOT_COMPLETE_CONTEXT, sentence):
                continue
            for tid, t in tasks.items():
                title = normalize(t["title"])
                if len(title) >= 6 and title in sentence and t["status"] != "done":
                    msg = f"Claims '{t['title']}' is complete, but its status is '{t['status']}'."
                    if msg not in violations and not any(f"task {tid} is complete" in v for v in violations):
                        violations.append(msg)
        # 3. commitments the system cannot make
        for pat in _COMMITMENT:
            if re.search(pat, low):
                violations.append("Contains a delivery or deadline commitment that project data does not support.")
                break
        # 4. dates that are not in the database
        for d in _ISO_DATE.findall(text):
            if d not in known_dates:
                violations.append(f"Mentions the date {d}, which is not in the project data.")

    violations = list(dict.fromkeys(violations))
    if violations:
        return GuardrailResult(False, "ungrounded_output", "The report contains unsupported statements.", violations)
    return GuardrailResult(True)
