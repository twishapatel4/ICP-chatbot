# ICP chatbot backend

Backend for the ICP collector chatbot: an adaptive interview that produces a company's Ideal
Customer Profile, with a scoring model, and logs everything needed to improve over time.

- Design: `../docs/superpowers/specs/2026-10-15-icp-chatbot-design.md`
- Cost reference: `../ICP Chatbot One-Month Cost Breakdown (100 Conversations) - Revised.pdf`
- Implementation plans: `../docs/superpowers/plans/`

## Setup

```bash
# This machine has no `python3-venv` package, so the venv is created without pip and pip is bootstrapped:
python3 -m venv --without-pip .venv
curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
# On a machine with python3-venv, `python3 -m venv .venv` is enough.

.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env        # then fill in values
```

## Run

```bash
# Database (Postgres): create the tables
PYTHONPATH=src .venv/bin/alembic upgrade head

# API (needs GEMINI_API_KEY, or Google Cloud credentials with LLM_BACKEND=vertex)
PYTHONPATH=src .venv/bin/uvicorn icp_chatbot.main:app --port 8000
```

The API trusts an `X-Tenant-Id` header set by LeadNest's gateway. Do not expose it to the internet.

## Commands

| Task | Command |
|---|---|
| Tests | `.venv/bin/python -m pytest` |
| Lint and format | `.venv/bin/ruff check src tests` and `.venv/bin/ruff format src tests` |
| Check the model names and thinking levels work (costs a few cents) | `PYTHONPATH=src .venv/bin/python -m icp_chatbot.cli.smoke_live` |
| Replay the evaluation cases against the real model | `PYTHONPATH=src .venv/bin/python -m icp_chatbot.cli.replay --out run.json` |
| Compare with an earlier run | `... -m icp_chatbot.cli.replay --baseline base.json` |
| Is the launch set ready? | `PYTHONPATH=src .venv/bin/python -m icp_chatbot.cli.launch_check` |
| Interview metrics | `PYTHONPATH=src .venv/bin/python -m icp_chatbot.cli.metrics --days 30` |
| Mark stale interviews abandoned (run hourly) | `PYTHONPATH=src .venv/bin/python -m icp_chatbot.cli.abandon` |

Write evaluation results outside `eval_cases/`: every `.json` file in that folder is read as a case.

## Testing without Postgres

Tests use in-memory SQLite and a scripted fake model, so they need no database server and no
credentials, and never call a real model.
