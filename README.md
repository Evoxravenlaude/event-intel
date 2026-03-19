# Event Intel Starter v5

A FastAPI backend for a geo-aware, cross-category event detection system.
Ingests signals from multiple sources, clusters them into deduplicated events,
and surfaces uncertain matches to a human review queue.

---

## What's included

- **FastAPI API** with events, signals, source runs, and review queue endpoints
- **SQLAlchemy models** for venues, organizers, events, raw signals, evidence, review queue, and source runs
- **Alembic migration** for the full schema
- **Radius search** on events with Haversine fallback (PostGIS-ready)
- **Signal clustering** with title, time, category, and geo scoring
- **Review queue moderation** — approve link, reject, recluster
- **Organizer reliability learning** — reliability scores adjust after each human review
- **Source ingestion adapters**
  - **Luma** via ICS feed or page URL
  - **Eventbrite** via private token or page URL fallback
  - **Telegram** via RSS/Atom feeds
  - **LinkedIn** via feed URLs
  - **X (Twitter)** via API v2 bearer token or RSS/Atom bridge feeds
- **Recurring scheduler** for automatic ingestion + clustering (configurable via env)
- **Paginated list endpoints** for events, signals, and review queue
- **Rich Pydantic response schemas** on every endpoint
- **Railway-ready** Docker deployment with `api`, `worker`, and `scheduler` services
- **Supabase** bootstrap SQL with PostGIS and RLS skeleton
- **Tests** covering health, CRUD, pagination, signal→event flow, clustering deduplication, and review resolution

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `POST` | `/events` | Create event directly |
| `GET` | `/events` | List events (paginated) — filter by `category`, `status`, `city`, `lat`/`lng`/`radius_km` |
| `GET` | `/events/{id}` | Get single event |
| `POST` | `/signals` | Submit raw signal |
| `GET` | `/signals` | List signals (paginated) — filter by `processed`, `source_type` |
| `POST` | `/signals/ingest` | Run source adapter ingestion |
| `POST` | `/signals/cluster` | Run clustering pass |
| `GET` | `/signals/runs` | List source runs |
| `GET` | `/review-queue` | List queue items (paginated) — filter by `status` |
| `GET` | `/review-queue/{id}` | Get single queue item |
| `POST` | `/review-queue/{id}/resolve` | Resolve a review item |

All list endpoints accept `page` (default 1) and `page_size` (default 50, max 200).

---

## Example requests

### Ingest
```json
POST /signals/ingest
{
  "source": "luma",
  "city": "Lagos",
  "query": "web3",
  "urls": ["https://example.com/calendar.ics"]
}
```

### Geo radius search
```
GET /events?lat=6.5244&lng=3.3792&radius_km=8
```

### Review actions
```json
POST /review-queue/42/resolve
{"action": "approve_link", "candidate_event_id": 12, "note": "same venue and schedule"}
```
```json
{"action": "reject", "note": "different event entirely"}
```
```json
{"action": "recluster"}
```

---

## Source adapters

### X (Twitter)
Set `X_BEARER_TOKEN` to use the API v2 recent-search endpoint. Without it, the
adapter falls back to RSS/Atom feed URLs (e.g. a nitter.net bridge) set in
`TELEGRAM_FEED_URLS` isn't the right setting — pass URLs directly in the
ingest request payload.

### LinkedIn / Telegram
Both use the `FeedAdapter` with RSS/Atom feed URLs configured via
`LINKEDIN_SOURCE_URLS` and `TELEGRAM_FEED_URLS` (comma-separated).

---

## Scheduler

The scheduler enables recurring ingestion without manual API calls.

```bash
python -m app.workers.scheduler
```

Configure via `SCHEDULER_SOURCES` — a JSON array of job definitions:

```
SCHEDULER_SOURCES='[
  {"source": "luma",       "city": "Lagos",  "query": "web3", "interval_minutes": 60},
  {"source": "eventbrite", "city": "Accra",  "query": "tech", "interval_minutes": 120},
  {"source": "telegram",   "city": "Lagos",                   "interval_minutes": 30}
]'
```

After each ingestion pass the scheduler automatically runs the clustering pass,
so new signals are resolved into events immediately.

---

## Supabase + Railway split

Use **Supabase** as the system of record for Postgres, Auth, Realtime, and PostGIS.
Use **Railway** for the API service, worker, and scheduler.

Three Railway services:
- `api` → `uvicorn app.main:app`
- `worker` → `python -m app.workers.run_once`
- `scheduler` → `python -m app.workers.scheduler`

---

## Running tests

```bash
pytest tests/ -v
```

---

## What is still not magically solved

- LinkedIn and X do not have turnkey open ingestion. This repo supports
  feed-driven integration for LinkedIn and API v2 for X; full LinkedIn API
  requires partner access.
- Geo normalization is still lightweight. A venue and neighborhood knowledge
  graph would improve clustering accuracy.
- Ongoing-event detection is confidence-based, not guaranteed truth.

## Good next upgrades

- PostGIS-native radius queries in SQL (replace Haversine Python fallback)
- Venue alias graph to deduplicate slight name variations
- Multilingual extraction (especially for Lagos/Accra use cases)
- Webhook push for newly confirmed high-confidence events
