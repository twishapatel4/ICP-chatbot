# Deploy: Render + Neon (free) and Vertex AI (pay per use)

Backend and frontend on Render, Postgres on Neon. Hosting is free; only Vertex AI calls cost money.

## 1. Database (Neon)
1. neon.tech -> sign up -> new project.
2. Copy the connection string. It looks like `postgresql://user:pass@host/db?sslmode=require`.
3. Change the prefix to `postgresql+psycopg://` (the app needs the driver name):
   `postgresql+psycopg://user:pass@host/db?sslmode=require`

## 2. Vertex AI service account
1. Google Cloud console -> pick the project -> enable the **Vertex AI API**.
2. IAM -> Service Accounts -> create one with role **Vertex AI User**.
3. Keys -> Add key -> JSON. Download it (keep it private, never commit it).
4. Note the project ID.

## 3. Push code
Push this repo (with `render.yaml`) to GitHub. `.env` stays out of git.

## 4. Render
1. render.com -> New -> Blueprint -> pick the repo.
2. Render reads `render.yaml`, creates `icp-chatbot-api` and `icp-chatbot-ui`.
3. When asked, fill values for the API:
   - `DATABASE_URL` = string from step 1
   - `GOOGLE_CLOUD_PROJECT` = project ID from step 2
4. Apply. First deploy runs `alembic upgrade head`, then starts the API.
5. Render -> `icp-chatbot-api` -> Environment -> **Secret Files** -> add file named
   `gcp-key.json`, paste the whole JSON from step 2. Save (it redeploys).

## 5. Check
- `https://icp-chatbot-api.onrender.com/health` -> `{"status":"ok"}`
- Open the `icp-chatbot-ui` URL, run an interview.

If Render gave the API a different URL (name was taken), edit `destination` in
`render.yaml` under `/api/*`, push again.

## 6. Keep it awake (optional)
Free API sleeps after ~15 min idle; first request then takes 30-60 s.
UptimeRobot (free) -> HTTP monitor on `/health`, every 10 min.
(Free Render hours: ~750/month, enough for one always-on service.)

## Limits to know
- Free API sleeps when idle. Fine for demo, not for customers.
- Vertex AI is billed per use (needs a billing-enabled GCP project). Set budget alerts
  in GCP. The app's daily token/session caps (`.env.example`) limit spend.
- Never use SQLite on Render free: disk is wiped on each deploy.
- Change tenant id: set `VITE_TENANT_ID` in the UI service env if not `demo`.
