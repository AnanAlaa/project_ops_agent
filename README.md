# AI Project Operations Agent

Agentic AI final project (Project 4). The LLM decides what to do; Python decides what is allowed.

## Setup
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env      # add your OPENAI_API_KEY (never commit .env)
    python scripts/seed_db.py
    python -m unittest discover -s tests

## Running the Web Interface

Launch the Gradio web UI:

```bash
python run_ui.py
```

Or via module:

```bash
python -m app.ui
```

Then open your browser at **`http://127.0.0.1:7860`**.

> **Note:** The UI runs in **Demo Mode** out-of-the-box using the smart simulator if no `OPENAI_API_KEY` is provided, with all Python guardrails, routing, approvals, and database operations fully active. Adding your key in `.env` automatically enables Live Mode with OpenAI agents.

## Code-orchestrated vs LLM-orchestrated decisions
(fill in as you build; the brief requires this section)

| Decision | Who decides |
|---|---|
| Input guardrail, routing, authorization, loop limit | Code |
| Task extraction, risk analysis, worker delegation, report writing, grounding check | LLM |
