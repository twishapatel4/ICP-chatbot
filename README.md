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
   degree and field of study once the sync script has produced those two lists (the card is hidden until then). Multi-select and range cards also accept the exclusive value
   `Any`, which builds no rule. Submit with `POST /sessions/{id}/profile`. The answers are stored
   as stated slots (confidence 0.9, turn 0), so the interview never asks them again. An incomplete
   form gets a 422 listing the missing slots, and a refresh at this step resumes with the answers
   kept. The chat endpoints return 409 `profile_incomplete` until the form is accepted.

   In API terms: `POST /sessions` (step 1; body carries `track`, the Sales/HR role dropdown), `GET /profile-form?track=`, `GET /options/{facet}`, then `POST /sessions/{id}/profile` (step 2), then the chat endpoints (step 3).
3. **Chat.** The same interviewer for both roles asks only what is still missing, then builds
   the document. The document carries the `track` and a `linkedin_filters` block (facet, ids,
   labels, unmapped values) that maps the profile to LinkedIn search filters.

| | Sales / BDE | HR |
|---|---|---|
| Output | Ideal Customer Profile | Ideal Candidate Profile |
| Form cards | industries, geography, buyer roles (the business model is seeded B2B and company size is asked in the chat) | role hiring for, experience level (Fresher, Experienced, Both), education, background (candidate location and must-have skills are asked in the chat) |
| Skipped when | nothing in the form; a B2C answer in the chat skips the industry and buyer questions | Fresher: background hidden; Experienced or Both: graduation year and field of study hidden |
| Scoring | 9 criteria, weights sum to 100: industry fit 25, company size 15, company type 5, company age 5, buying signals 10, geographic fit 20, buyer fit 10, seniority 5, department 5 | 11 criteria, weights sum to 100: role 20, function 5, seniority 5, experience 15, skills 15, certifications 5, education 10, school 5, location 10, background 5, employer 5 |

Seniority, function and any other criterion guessed from a job title weigh at most 5 (the derived criteria). The education card stays hidden until `scripts/sync_linkedin_catalogs.py` has produced `degrees.json` and `fields_of_study.json`. Sessions saved before the steps existed load as `track:
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

`BACKGROUND_ENRICHMENT` (env var, default true, false automatically when `VERCEL` is set) controls whether the step 1 website read runs in the background while the user fills step 2. When false, nothing starts at step 1 and step 2 reads the site inline. Serverless functions freeze after the response is sent, so a background task would never finish there.

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
form); without them it uses the legacy one-call `start` (Sales, no form). `case_problems` flags
unknown keys in `profile_answers` and any answer the form would reject. `hr_*.json` cover
Fresher, Experienced, Both and vague HR briefs. `b2c_retail.json` is the chat-only B2C case: the Sales form
does not ask the business model, so B2C can only come from the chat.

**HR cases leave out education.** The education card is hidden until the degree and
field-of-study snapshots exist. After `scripts/sync_linkedin_catalogs.py` has been run the card
becomes required, the HR cases will then fail `case_problems` (and `test_hr_cases_load_and_use_hr_slots`),
and their `profile_answers` need the education slots added.

**Release gate.** `... -m icp_chatbot.cli.replay --baseline base.json` exits 1 when a candidate
regresses against a saved run. The gate as first run for this branch failed (mean F1 and injection
F1 down, one forbidden-value violation). Triage found no product regression: the
`misleading_website` violation is already present at the baseline; `detail_mode_user` was a single
noisy run; injection violations are 0 at every commit. The F1 drops were measurement artifacts,
because the scorer compared free text with values the product now stores as LinkedIn labels. The
scorer is catalog-aware since commit `7d90b7a` ("fix: catalog-aware eval scoring and LinkedIn-label
expectations"); older run files and baselines must be re-recorded. **A fresh live gate run with the
new scorer has not been done yet.** One known pre-existing issue stays open: website-seeded
industries stay as target industries when the user sells something else. Details:
[docs/eval-triage-2026-10-29.md](docs/eval-triage-2026-10-29.md).

**Eval scoring is catalog-aware** (`src/icp_chatbot/eval/match.py`): geography and company-name
slots use the same matching as production scoring, industries match by LinkedIn id or hierarchy
(Banking is inside Financial Services), seniority, function and size through their catalogs, and
everything else by `loose_equal`; case expectations are written in LinkedIn labels. Run files and
baselines recorded before this change are not comparable with the new scores: re-record the baseline.

## Testing without Postgres

Tests use in-memory SQLite and a scripted fake model, so they need no database server and no
credentials, and never call a real model.

## Frontend

React chat screen in `frontend/` (see `frontend/README.md`): `cd frontend && npm install && npm run dev`, with the API running on port 8000.
