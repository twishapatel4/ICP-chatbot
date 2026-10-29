# ICP chatbot backend

Backend for the ICP collector chatbot: an adaptive interview that produces a company's Ideal
Customer Profile (Sales/BDE) or Ideal Candidate Profile (HR), with a scoring model, and logs
everything needed to improve over time.

- Design: `../docs/superpowers/specs/2026-10-15-icp-chatbot-design.md`
- Cost reference: `../ICP Chatbot One-Month Cost Breakdown (100 Conversations) - Revised.pdf`
- Implementation plans: `../docs/superpowers/plans/`

## Flow

A session has three steps and one of two roles (`track`).

1. **About you.** `POST /sessions` with the company, the contact and `track` (`"sales"`, the
   default, or `"hr"`). It returns the session id, `step: "profile"` and the step 2 cards. The
   website is read in the background meanwhile.
2. **Profile form.** A short set of cards (`GET /profile-form?track=`). Every option is a
   LinkedIn option, served by `GET /options/{facet}` from versioned snapshots in
   `src/icp_chatbot/linkedin/data/`: industry (434), company size (9), seniority (10), job
   function (35), country (ISO 3166-1, cities as free-text chips under it), and for HR also
   degree and field of study. Multi-select and range cards also accept the exclusive value
   `Any`, which builds no rule. Submit with `POST /sessions/{id}/profile`. The answers are stored
   as stated slots (confidence 0.9, turn 0), so the interview never asks them again. An incomplete
   form gets a 422 listing the missing slots, and a refresh at this step resumes with the answers
   kept. The chat endpoints return 409 `profile_incomplete` until the form is accepted.
3. **Chat.** The same interviewer for both roles asks only what is still missing, then builds
   the document. The document carries the `track` and a `linkedin_filters` block (facet, ids,
   labels, unmapped values) that maps the profile to LinkedIn search filters.

| | Sales / BDE | HR |
|---|---|---|
| Output | Ideal Customer Profile | Ideal Candidate Profile |
| Form cards | business model, industries, company size, geography, buyer roles | role hiring for, experience level (Fresher, Experienced, Both), education, background, candidate location, must-have skills |
| Skipped when | B2C: industry, company size and buyer cards are hidden and not required | Fresher: background hidden; Experienced or Both: graduation year and field of study hidden |
| Scoring | six default criteria, weights sum to 100 | Role 25, Experience 20, Skills 20, Education 15, Location 10, Background 10 |

The education card stays hidden until the degree and field-of-study snapshots exist; build them
with `scripts/sync_linkedin_catalogs.py`. Sessions saved before the steps existed load as `track:
"sales"`, `step: "chat"` and keep working.

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
| Refresh the HR degree and field-of-study lists (needs a LinkedIn API token) | `LINKEDIN_TOKEN=... .venv/bin/python scripts/sync_linkedin_catalogs.py --token-env LINKEDIN_TOKEN` (the two generated files in `src/icp_chatbot/linkedin/data/` are committed deliberately after a real run) |
| Mark stale interviews abandoned (run hourly) | `PYTHONPATH=src .venv/bin/python -m icp_chatbot.cli.abandon` |

Write evaluation results outside `eval_cases/`: every `.json` file in that folder is read as a case.

An evaluation case sets `track` (`"sales"` by default) and may carry `profile_answers`, the step 2
form. With them the replay runs the real three-step flow (`create`, then `begin_chat` with the
form); without them it uses the legacy one-call `start` (Sales, no form). `hr_*.json` cases
cover Fresher, Experienced, Both and vague HR briefs; `b2c_retail.json` runs the B2C form.

**Release gate.** Prompt changes must pass the replay against a saved baseline before release:
`... -m icp_chatbot.cli.replay --baseline base.json` exits 1 on a regression. A failing gate blocks
the release, not the merge.

## Testing without Postgres

Tests use in-memory SQLite and a scripted fake model, so they need no database server and no
credentials, and never call a real model.

## Frontend

React chat screen in `frontend/` (see `frontend/README.md`): `cd frontend && npm install && npm run dev`, with the API running on port 8000.
