# AI Project Operations Agent

Agentic AI final project (Project 4). The LLM decides what to do; Python decides what is allowed.

## Setup
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env      # add your OPENAI_API_KEY (never commit .env)
    python scripts/seed_db.py
    python -m unittest discover -s tests

## Code-orchestrated vs LLM-orchestrated decisions
(fill in as you build; the brief requires this section)

| Decision | Who decides |
|---|---|
| Input guardrail, routing, authorization, loop limit | Code |
| Task extraction, risk analysis, worker delegation, report writing, grounding check | LLM |
