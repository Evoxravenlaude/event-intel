# Deployment Guide

## Stack

| Layer | Service |
|---|---|
| API + workers | Railway |
| Database | Supabase (PostgreSQL + PostGIS + pgvector) |
| Schema migrations | Alembic (runs on startup via bootstrap.sh) |

---

## First-time setup

### 1. Supabase

1. Create a new Supabase project.
2. In the SQL editor, run `supabase/bootstrap.sql`. This enables PostGIS, pgvector, and sets up RLS policies.
3. Copy the **connection string** from Project Settings → Database → URI. Use the `postgresql://` (not `postgres://`) form with the `?sslmode=require` suffix.

### 2. Environment variables

Set these on every Railway service that needs them:

```
# Required
DATABASE_URL=postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres?sslmode=require
APP_ENV=production
DEBUG=false

# Strongly recommended
API_KEY=<generate with: openssl rand -hex 32>

# Optional — tighten CORS to your frontend domain
CORS_ORIGINS_RAW=https://yourapp.com

# Optional — fire webhooks when events are confirmed
WEBHOOK_URLS=https://yourapp.com/api/webhooks/event-intel

# Optional source credentials
EVENTBRITE_PRIVATE_TOKEN=
X_BEARER_TOKEN=
LUMA_FEED_URLS=
TELEGRAM_FEED_URLS=
LINKEDIN_SOURCE_URLS=

# Optional scheduler
SCHEDULER_SOURCES=[{"source":"luma","city":"Lagos","query":"web3","interval_minutes":60}]

# Embeddings (model downloaded on first cold start — ~50MB)
EMBEDDINGS_ENABLED=true
EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2

# Backfill (only needed once after enabling embeddings on existing data)
BACKFILL_BATCH_SIZE=100
```

### 3. Railway services

Create three services from the same repo:

| Service | Start command |
|---|---|
| `api` | `sh scripts/bootstrap.sh` |
| `scheduler` | `python -m app.workers.scheduler` |
| `worker` | `python -m app.workers.run_once` (one-shot, trigger manually) |

The `api` service runs `alembic upgrade head` before starting uvicorn (see `scripts/bootstrap.sh`). The scheduler and worker services share the same image — they do not run migrations themselves.

### 4. First deploy sequence

```
1. Deploy api service      → Alembic runs 0001 → 0002 → 0003, then server starts
2. Verify /health returns  {"ok": true}
3. Run bootstrap.sql       → PostGIS + pgvector extensions + RLS policies
4. (Optional) Trigger worker service once to run an initial clustering pass
5. (Optional) If you have existing data, trigger backfill service:
   python -m app.workers.backfill_embeddings
```

---

## Ongoing operations

### Adding a new source

```json
POST /signals/ingest
{"source": "luma", "city": "Accra", "query": "tech", "urls": [...]}
```

Then trigger a cluster pass:

```
POST /signals/cluster
```

Or let the scheduler do it automatically on its configured interval.

### Reviewing uncertain matches

```
GET /review-queue?status=pending
POST /review-queue/{id}/resolve  {"action": "approve_link", "candidate_event_id": 42}
POST /review-queue/{id}/resolve  {"action": "reject"}
POST /review-queue/{id}/resolve  {"action": "recluster"}
```

### Running migrations after a code update

`bootstrap.sh` runs `alembic upgrade head` automatically on every deploy.
To run manually:

```bash
DATABASE_URL=... alembic upgrade head
```

### Rolling back a migration

```bash
DATABASE_URL=... alembic downgrade -1
```

---

## Security checklist before going live

- [ ] `API_KEY` is set and not committed to git
- [ ] `CORS_ORIGINS_RAW` is set to your frontend domain (not `*`)
- [ ] `DEBUG=false` and `APP_ENV=production`
- [ ] `ENABLE_MOCK_ADAPTERS=false`
- [ ] Supabase RLS is enabled (bootstrap.sql was run)
- [ ] `DATABASE_URL` uses `?sslmode=require`
- [ ] `.env` is in `.gitignore`

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — defaults use SQLite, no Alembic run needed
uvicorn app.main:app --reload
```

API docs available at http://localhost:8000/docs (disabled in production).

---

## Frontend (Meridian)

The Meridian frontend is served directly by the FastAPI app at `/`.

**How it works:**
- `app/static/index.html` is the complete Meridian SPA.
- On load, it auto-detects the API by calling `/health` on the same origin.
- If the health check passes, it switches from demo mode to live data automatically.
- No separate frontend deployment needed.

**First visit after deploy:**

Go to `https://your-app.onrender.com/` — the Meridian interface loads and connects to the live API automatically. No configuration required.

**Updating the frontend:**

Replace `app/static/index.html` with the new file and redeploy. Render rebuilds the Docker image and the new frontend is served immediately.

**Standalone frontend (optional):**

To host the frontend separately (e.g. on Netlify or GitHub Pages):
1. Open `app/static/index.html` in a browser.
2. Paste your Render API URL into the "Event Intel API" field in the sidebar.
3. Click →. The frontend connects and loads live data.

The auto-detection only fires when served from the same origin as the API.
