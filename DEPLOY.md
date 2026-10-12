# Deployment Guide

## Stack

| Layer | Service |
|---|---|
| API + workers | Railway (Docker) |
| Database | Supabase (PostgreSQL + PostGIS + pgvector) |
| Schema migrations | Alembic — runs on API boot via `scripts/bootstrap.sh` |
| Frontend | Served by the API at `/` (no separate deploy) |

---

## First-time setup

### 1. Supabase

1. Create a project.
2. Project Settings → Database → copy the **direct** connection string
   (`postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres`). Append `?sslmode=require`.
   The app rewrites the scheme to `postgresql+psycopg://` itself.
3. Wait for the first API deploy (step 4) so the tables exist, then run
   `supabase/bootstrap.sql` in the SQL editor. It enables PostGIS + pgvector,
   enables row-level security on **every** table (Supabase's REST API exposes any
   table without RLS to anyone holding the anon key), and repairs the spatial /
   vector columns if the extensions weren't available when the migration ran.

### 2. Environment variables

Set on every Railway service (shared variables work well):

```
# Required
DATABASE_URL=postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres?sslmode=require
APP_ENV=production
DEBUG=false
ENABLE_MOCK_ADAPTERS=false

# Strongly recommended
API_KEY=<openssl rand -hex 32>
CORS_ORIGINS_RAW=https://yourapp.com

# Live global discovery — at least one of these, or /discover has nothing to scan
TICKETMASTER_API_KEY=<developer.ticketmaster.com>
SEATGEEK_CLIENT_ID=<platform.seatgeek.com>
GEOCODER_USER_AGENT=event-intel/1.1 (ops@yourapp.com)

# Location accuracy — optional but recommended (see "Google Maps keys" below)
GOOGLE_MAPS_API_KEY=<server key: Geocoding API + Places API (New)>
GOOGLE_MAPS_BROWSER_KEY=<browser key: Maps JavaScript API, referrer-restricted>
PUBLIC_READ=true
PUBLIC_SCANS_PER_HOUR=30

# Optional
WEBHOOK_URLS=https://yourapp.com/api/webhooks/event-intel
WEBHOOK_SECRET=<openssl rand -hex 32>
EMBEDDINGS_ENABLED=true
SCHEDULER_SOURCES=[{"source":"coverage","interval_minutes":120},{"source":"luma","city":"Lagos","query":"web3","interval_minutes":60,"urls":["https://lu.ma/....ics"]}]

# Source credentials (see README → Source adapters)
EVENTBRITE_PRIVATE_TOKEN=
EVENTBRITE_ORGANIZATION_ID=
LUMA_FEED_URLS=
TELEGRAM_FEED_URLS=
LINKEDIN_SOURCE_URLS=
X_BEARER_TOKEN=
X_FEED_URLS=
```

### Google Maps keys (optional)

Without keys everything still works: Nominatim (geocoding), Photon (place search) and
Leaflet + OpenStreetMap (maps). Google is more accurate for street addresses and venue
names, especially in Lagos/Nairobi-style cities where OSM is thin.

1. Google Cloud console → enable **Geocoding API**, **Places API (New)** and **Maps JavaScript API**.
2. Create key #1 → *API restrictions*: Geocoding API + Places API (New). Set it as
   `GOOGLE_MAPS_API_KEY`. It never leaves the server.
3. Create key #2 → *API restrictions*: Maps JavaScript API; *Application restrictions*:
   HTTP referrers `https://event-intel-production.up.railway.app/*` (and your own domain).
   Set it as `GOOGLE_MAPS_BROWSER_KEY`. The page receives it from `/config`.
4. If the browser key is rejected, the page switches to OpenStreetMap on its own.

Directions always deep-link to Apple Maps (primary on iPhone/iPad/Mac) and Google Maps
(primary elsewhere); neither needs a key.

### 3. Railway services

Create the services from the same repo. `railway.json` sets the build to the
Dockerfile and the **default** start command to `bash scripts/bootstrap.sh`
(migrate, then serve). Override the start command per service in the Railway
dashboard (Service → Settings → Deploy → Custom Start Command):

| Service | Start command | Notes |
|---|---|---|
| `api` | `bash scripts/bootstrap.sh` | Runs `alembic upgrade head` then uvicorn. Healthcheck `/health`. |
| `scheduler` | `python -m app.workers.scheduler` | Needs `SCHEDULER_SOURCES`. Keep `SCHEDULER_IN_PROCESS=false` on the api. |
| `worker` (optional, one-off) | `python -m app.workers.run_once --loop` | Drain a cluster backlog. |
| `backfill` (optional, one-off) | `python -m app.workers.backfill_embeddings` | Only after enabling embeddings on existing data. |

Single-service deploy: skip `scheduler` and set `SCHEDULER_IN_PROCESS=true` on `api`.

The image is ~2GB because of torch. To build a slim image without embeddings
(Jaccard fallback), set the Railway build arg `WITH_EMBEDDINGS=false`.

### 4. First deploy sequence

```
1. Deploy api              → bootstrap.sh runs migrations 0001 → 0004, starts uvicorn
2. GET /health             → {"ok": true, ...}
3. GET /ready              → {"ok": true, "database": "ok", "schema": "ok", ...}
4. Run supabase/bootstrap.sql in the Supabase SQL editor
5. Open https://<api-domain>/  → frontend auto-connects; enter the API key in Control
6. (Optional) POST /signals/ingest … then /signals/cluster, or let the scheduler do it
```

The embedding model (~120MB) downloads in a background thread on first boot —
`/ready` reports `"embeddings": "loading"` until it's cached. Clustering falls back
to Jaccard until then, so nothing blocks.

---

## Ongoing operations

### Adding a source

```json
POST /signals/ingest
{"source": "luma", "city": "Accra", "query": "tech", "urls": ["https://lu.ma/accra-tech.ics"]}
```

followed by `POST /signals/cluster`, or add it to `SCHEDULER_SOURCES`.

### Reviewing uncertain matches

Use the **Review Queue** panel in the frontend's Control tab, or:

```
GET  /review-queue                                  (pending by default)
POST /review-queue/{id}/resolve  {"action": "approve_link"}
POST /review-queue/{id}/resolve  {"action": "reject"}      → seeds a separate event
POST /review-queue/{id}/resolve  {"action": "discard"}     → noise
POST /review-queue/{id}/resolve  {"action": "recluster"}
```

### Migrations

`bootstrap.sh` runs `alembic upgrade head` on every api deploy. Manually:

```bash
DATABASE_URL=... alembic upgrade head
DATABASE_URL=... alembic downgrade -1
```

Set `RUN_MIGRATIONS=false` on the api service if you prefer to run them yourself.

### Observability

- Production logs are one JSON object per line (`time, level, logger, msg`, plus
  `request_id, path, method, status_code, duration_ms` on request lines).
- Every response carries `X-Request-ID` (echoed if the client sent one).
- `GET /stats` is a cheap pulse: pending reviews, unprocessed signals, last run per source.

---

## Security checklist before going live

- [ ] `API_KEY` set; never committed
- [ ] `CORS_ORIGINS_RAW` is your frontend domain, not `*`
- [ ] `DEBUG=false`, `APP_ENV=production` (hides Swagger / OpenAPI)
- [ ] `ENABLE_MOCK_ADAPTERS=false`
- [ ] `supabase/bootstrap.sql` has been run (RLS on all tables)
- [ ] `DATABASE_URL` uses `?sslmode=require`
- [ ] `WEBHOOK_SECRET` set if you consume webhooks
- [ ] Only one scheduler running (service **or** in-process)
- [ ] Google keys (if used): server key restricted to Geocoding + Places, browser key referrer-restricted
- [ ] Railway domain targets the port the app listens on (`$PORT`, 8080 on Railway)
- [ ] `GEOCODER_USER_AGENT` names a real contact (Nominatim policy) — or point `GEOCODER_BASE_URL` at your own instance

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # SQLite, no migrations needed (create_all on boot)
uvicorn app.main:app --reload
pytest
```

Docker, mirroring production:

```bash
docker build --build-arg WITH_EMBEDDINGS=false -t event-intel .
docker run -p 8000:8000 -e DATABASE_URL=sqlite:////tmp/ei.db -e EMBEDDINGS_ENABLED=false event-intel
```

---

## Frontend

`app/static/index.html` is the whole frontend: phones get a bottom tab bar,
tablets a centred column with a floating tab bar and dialog sheets, desktops a
left rail with the feed and a live map side by side. It installs as a PWA
(`/manifest.webmanifest`). On load it reads `/config` (map provider, browser key),
asks for a precise GPS fix (falls back to an IP-based city if denied) and calls
`/discover`. The public feed needs no key; the Control tab's admin views
(signals, review queue) appear once you enter the API key there.

To host it elsewhere (Netlify, GitHub Pages): open the file, paste the API URL and
key in the Control tab, press Connect. Set `CORS_ORIGINS_RAW` to that host.
