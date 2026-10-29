# Deploy: Vercel (backend + frontend) + Neon + Vertex AI

Two Vercel projects from this one repo, Postgres on Neon, models on Vertex AI.
Hosting is free (Hobby); only Vertex AI calls cost money.

## 1. Database (Neon)
1. neon.tech -> new project. Connect -> branch `production`, **Pooled connection** on.
2. Copy the string and change its prefix to `postgresql+psycopg://`:
   `postgresql+psycopg://user:pass@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require`
3. Create the tables once, from your machine (Vercel does not run migrations):
   ```bash
   DATABASE_URL='<string above>' PYTHONPATH=src .venv/bin/alembic upgrade head
   ```
   Repeat after any new migration.

## 2. Vertex AI service account
1. Google Cloud -> enable **Vertex AI API** (billing on).
2. IAM -> Service Accounts -> create one with role **Vertex AI User**.
3. Keys -> Add key -> JSON. Keep the file private; never commit it.

## 3. Backend (Vercel project 1)
1. vercel.com -> Add New -> Project -> import the repo, branch `staging`.
2. **Root Directory** = repo root (leave blank). Framework preset: Other.
3. Environment variables:
   - `LLM_BACKEND` = `vertex`
   - `GOOGLE_CLOUD_PROJECT` = your project ID
   - `GOOGLE_CLOUD_LOCATION` = `global`
   - `GOOGLE_CREDENTIALS_JSON` = the whole contents of the JSON key
   - `DATABASE_URL` = Neon string from step 1
   - `AUTO_CREATE_TABLES` = `false`
   - `BACKGROUND_ENRICHMENT` = leave unset: it defaults to true, but is switched to false automatically when `VERCEL` is set. With false, the step 1 website read is not started in the background and step 2 reads the site inline, because a serverless function freezes after it responds and a background task would never finish.
4. Deploy. Check `https://<project>.vercel.app/health` -> `{"status":"ok"}`.
5. Name the project `icp-chatbot-api` for the URL below, or edit `frontend/vercel.json`.

## 4. Frontend (Vercel project 2)
1. Add New -> Project -> same repo, **Root Directory** = `frontend` (Vite is auto-detected).
2. `frontend/vercel.json` forwards `/api/*` to the backend URL (no CORS needed).
   If the backend URL is not `https://icp-chatbot-api.vercel.app`, edit it and push.
3. Optional env var `VITE_TENANT_ID` if not `demo`.
4. Deploy, open the URL, run an interview.

## Limits to know
- Hobby plan is non-commercial only. Customer-facing use needs Vercel Pro.
- Function time limit is set to 60 s (`vercel.json`). Website enrichment can take up to
  40 s, so a slow site plus a slow model call could time out.
- The per-minute rate limit is in memory, so it applies per function instance, not globally.
  The daily DB-backed session/token caps still hold.
- Vertex AI is billed per use. Set budget alerts in GCP.
- Never use SQLite on Vercel: the filesystem is read-only/ephemeral.
