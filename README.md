# ICP chatbot backend

Backend for the ICP collector chatbot. Design: `../docs/superpowers/specs/2026-10-15-icp-chatbot-design.md`.
Cost reference: `../ICP Chatbot One-Month Cost Breakdown (100 Conversations) - Revised.pdf`.

## Setup

```bash
# This machine has no `python3-venv` package, so the venv is created without pip and pip is bootstrapped:
python3 -m venv --without-pip .venv
curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
# On a machine with python3-venv, `python3 -m venv .venv` is enough.

.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env        # then fill in values
```

## Everyday commands

```bash
.venv/bin/python -m pytest          # tests
.venv/bin/ruff check src tests      # lint
.venv/bin/ruff format src tests     # format
```

## Not installed yet

- PostgreSQL (and the pgvector extension, needed only for the later example-retrieval plan).
  This machine has no Docker or Postgres; tests that need a database are written against a
  Postgres URL in `DATABASE_URL` once one is available.
