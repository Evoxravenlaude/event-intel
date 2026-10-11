# Event Intel

Open the app anywhere in the world and see what's happening around you.

A FastAPI backend plus a bundled single-page frontend ("Meridian") for
location-first, cross-category event discovery. A request for a point on the
map triggers a live scan of geo-capable sources for that area, signals are
clustered into deduplicated events with a confidence score, and uncertain
matches go to a human review queue. Areas people look at are kept fresh by the
scheduler.

```
            GET /discover?lat&lng&radius_km
                      │
          ┌───────────▼───────────┐   no recent scan covers this circle
          │  coverage (scan areas) ├──────────────────────────────┐
          └───────────┬───────────┘                              ▼
                      │ fresh                    geo sources (ticketmaster, seatgeek, …)
                      │                                           │
                      ▼                                           ▼
                   events  ◀── cluster ◀── raw_signals ◀── /signals/ingest ◀── per-city feeds
                      │                        │                              (luma, eventbrite,
                      ▼                        ▼                               telegram, linkedin, x)
               radius query             review_queue ─▶ approve / reject / discard / recluster
```

---

## What's included

- **Location-first discovery** — `/discover` takes a point + radius, reverse-geocodes it, scans geo-capable sources if the area isn't fresh, clusters, and returns what's nearby with a coverage report. Scan circles are recorded so the next person in that city gets an instant answer
- **Geocoding** — Nominatim (no key) with a DB cache and an offline gazetteer fallback; venues that arrive as text get coordinates, venues that arrive as coordinates get a city/country
- **Global sources** — Luma city feeds (~80 cities, no key), Ticketmaster Discovery (30+ countries) and SeatGeek answer "within N km of here" directly; the per-city feed sources (Luma, Eventbrite, Telegram, LinkedIn, X) still work for communities those platforms don't cover
- **API** — events, signals, source runs, review queue, dashboard stats; all list endpoints paginated and filterable
- **Clustering** — embedding cosine similarity (multilingual MiniLM, pgvector-indexed on Postgres) with Jaccard fallback, plus category / time / distance boosts; hard distance/time vetoes so "Jazz Night" in two cities never merges; configurable thresholds
- **Ten categories** — web3, tech, business, culture, sports, food, nightlife, wellness, community, education — inferred from multilingual keywords (English, French, Pidgin, Swahili, Yoruba) and from source taxonomies
- **Review queue** — each item carries both the signal and the candidate event; four actions with well-defined outcomes
- **Organizer reliability** — nudged by review outcomes, feeds back into confidence
- **Geo** — radius search via PostGIS `ST_DWithin` on Postgres, bounding box + Haversine elsewhere; fuzzy venue dedup with alias table
- **Source adapters** — Luma (ICS / page JSON-LD), Eventbrite (org API / page JSON-LD), Telegram + LinkedIn (RSS/Atom, LinkedIn event pages), X (API v2 / RSS bridges)
- **Scheduler** — recurring ingest + cluster, standalone or in-process
- **Webhooks** — fired once when an event first becomes confirmed; optional HMAC signature
- **Frontend** — served at `/`; real geolocation → `/discover`, city search for any place on Earth via `/geocode`, radius slider, coverage status line, map centred on you, review-queue panel
- **Ops** — `/health` (liveness), `/ready` (DB + schema), JSON logs in production, request IDs, Alembic migrations run on boot, Docker + Railway + Supabase setup, GitHub Actions CI
- **Tests** — 161 tests covering the API, discovery/coverage, geocoding, clustering/review semantics, parsing, adapters (mocked HTTP), auth and geo

See `AUDIT.md` for what was broken in the previous version and how it was fixed.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt     # or requirements.txt without pytest
cp .env.example .env                    # defaults: SQLite, mocks on, embeddings on
uvicorn app.main:app --reload
```

Open http://localhost:8000/ for the frontend, http://localhost:8000/docs for Swagger.

To skip the ~1GB torch download in dev, set `EMBEDDINGS_ENABLED=false` and install
with `grep -v requirements-ml requirements.txt | pip install -r /dev/stdin`.

Run tests:

```bash
pytest
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Readiness — database reachable, schema present, embedding model state |
| `GET` | `/stats` | Totals, pending review, areas scanned, geo-source status, counts by category / city / source |
| `GET` | `/discover` | **Events around a point.** `lat`, `lng`, `radius_km` (default 25, max 200), `q`, `category`, `include_past`, `refresh` (force scan), `scan=false` (read-only) |
| `POST` | `/discover/scan` | Force a scan `{lat, lng, radius_km?, q?}`; returns the coverage report |
| `GET` | `/discover/coverage` | Recently scanned areas (circles) |
| `GET` | `/geocode?q=` | Place search → coordinates (city picker) |
| `GET` | `/geocode/reverse?lat&lng` | Coordinates → city / country |
| `POST` | `/events` | Create event (201) |
| `GET` | `/events` | List — `q`, `category`, `status` (comma list), `city`, `min_confidence`, `starts_after/before`, `lat`+`lng`+`radius_km`, `sort=start_time|confidence|created` |
| `GET` | `/events/{id}` | Get one |
| `PATCH` | `/events/{id}` | Partial update (title, category, status, times, venue, organizer) |
| `DELETE` | `/events/{id}` | Delete; its signals return to the unprocessed pool |
| `POST` | `/signals` | Submit raw signal (201). Same `(source_type, external_id)` updates in place |
| `GET` | `/signals` | List — `q`, `processed`, `source_type`, `category` |
| `GET` | `/signals/{id}` | Get one |
| `POST` | `/signals/ingest` | Run a source adapter: `{source, city?, query?, urls?, lat?, lng?, radius_km?}` |
| `POST` | `/signals/cluster` | Cluster unprocessed signals (`?limit=`); returns `remaining_unprocessed` |
| `GET` | `/signals/runs` | Source runs (paginated) — `source`, `status` |
| `GET` | `/review-queue` | Pending items by default; `status=all|approved|rejected|discarded|reclustered` |
| `GET` | `/review-queue/{id}` | Get one, with `signal` and `candidate_event` embedded |
| `POST` | `/review-queue/{id}/resolve` | `{action, note?, candidate_event_id?}` |

All list endpoints accept `page` (default 1) and `page_size` (default 50, max 200)
and return `{total, page, page_size, items}`.

Authentication: set `API_KEY`; send `X-API-Key: <key>` or `Authorization: Bearer <key>`.
`/`, `/static/*`, `/health` and `/ready` never require a key.

### Review actions

| Action | Meaning | Effect |
|---|---|---|
| `approve_link` | Signal is about the candidate event | Linked as evidence, event rescored, organizer rewarded |
| `reject` | Signal is a real event, but a *different* one | New event seeded from the signal, candidate's organizer penalised |
| `discard` | Signal is noise | Marked processed; nothing created |
| `recluster` | Re-evaluate (e.g. after you fixed the event via `PATCH`) | Signal re-enters clustering immediately |

### `/discover` response

```json
{
  "center": {"latitude": 6.52, "longitude": 3.38, "city": "Lagos", "country": "Nigeria", "country_code": "NG", "source": "geocoder"},
  "radius_km": 25, "total": 14, "page": 1, "page_size": 50, "items": [ …EventOut… ],
  "coverage": {
    "scanned": true, "reason": "never_scanned",        // fresh | stale | never_scanned | forced | no_sources
    "sources_run": ["ticketmaster"], "sources_skipped": ["seatgeek"],
    "signals_created": 18, "events_created": 14, "signals_linked": 4, "queued_for_review": 0,
    "errors": [], "last_scanned_at": "2026-10-10T17:40:12Z"
  }
}
```

A live scan is synchronous and bounded (~25 s of source time). `reason: no_sources`
means no geo-capable source has credentials — set `TICKETMASTER_API_KEY` and/or
`SEATGEEK_CLIENT_ID`. Per-city feed sources never run from `/discover`; they are
ingested by the scheduler and show up in the same radius query.

---

## Clustering

Each unprocessed signal is scored against candidate events:

```
title similarity (0–1, cosine of embeddings or Jaccard)
+ 0.10  same category
+ 0.20  start times within CLUSTER_TIME_WINDOW_HOURS
+ 0.25  venues within CLUSTER_DISTANCE_KM
```

Hard vetoes come first: both sides geolocated and > `CLUSTER_MAX_LINK_DISTANCE_KM`
(50) apart, or both timed and > `CLUSTER_MAX_LINK_TIME_DAYS` (3) apart → never the
same event. Category and distance boosts only apply once there is some title or
time evidence, so two concerts at one venue don't flood the review queue.

| Score | Outcome |
|---|---|
| ≥ `CLUSTER_LINK_THRESHOLD` (0.55) | auto-link to best event |
| ≥ `CLUSTER_REVIEW_THRESHOLD` (0.35) | human review |
| below | new event seeded from the signal |

Candidates come from the pgvector HNSW index on Postgres; elsewhere every event within
±`CLUSTER_CANDIDATE_WINDOW_DAYS` of the signal's time. A pass handles at most
`CLUSTER_BATCH_SIZE` signals; call again (or `run_once --loop`) to drain a backlog.

---

## Source adapters

| Source | Live input | Notes |
|---|---|---|
| `luma` | Nothing — keyless. Or explicit `.ics` / page URLs | **Geo-capable.** Luma runs a curated page + iCal feed for ~80 cities (Lagos, Nairobi, London, NYC, Singapore, …). `/discover` picks the Luma cities near the point and pulls their feeds; the city directory is fetched live from Luma daily, with a built-in snapshot as fallback |
| `ticketmaster` | `TICKETMASTER_API_KEY` (free) | **Geo-capable.** Discovery API v2: concerts, sports, theatre, family in 30+ countries. Backbone of `/discover` |
| `seatgeek` | `SEATGEEK_CLIENT_ID` (free) | **Geo-capable.** Strongest in North America; complements Ticketmaster |
| `luma` | `.ics` feed URLs, or Luma page URLs | Every Luma calendar has an ICS feed. Pages are parsed from their schema.org JSON-LD |
| `eventbrite` | `EVENTBRITE_PRIVATE_TOKEN` + `EVENTBRITE_ORGANIZATION_ID`, or public event page URLs | Eventbrite retired its public search API in 2020; the API only lists events your org owns. Page URLs need no token |
| `telegram` | RSS/Atom feed URLs (`TELEGRAM_FEED_URLS` or payload `urls`) | e.g. an RSSHub bridge for a public channel |
| `linkedin` | RSS/Atom feeds, or `linkedin.com/events/...` page URLs | No public API; pages are parsed from JSON-LD |
| `x` | `X_BEARER_TOKEN` (API v2 recent search), or RSS bridge URLs in `X_FEED_URLS` / payload | Searches all languages unless `X_LANGUAGES` is set |

With `ENABLE_MOCK_ADAPTERS=true` a source that yields nothing *and* hit no errors
returns one clearly-labelled mock item so the pipeline can be exercised. Turn this
off in production.

`urls` in the ingest payload must be `http(s)` and override configured env URLs.

---

## Scheduler

Jobs can target a city (feed sources), a point (geo sources), or `"coverage"`
— which re-scans every area someone looked at in the last `COVERAGE_RESCAN_DAYS`.

```bash
python -m app.workers.scheduler              # long-running
python -m app.workers.run_once --ingest      # run every job once, then cluster
python -m app.workers.run_once --loop        # drain the cluster backlog
```

```
SCHEDULER_SOURCES='[
  {"source": "luma",       "city": "Lagos",  "query": "web3", "interval_minutes": 60,
   "urls": ["https://lu.ma/your-calendar.ics"]},
  {"source": "telegram",   "city": "Accra",  "interval_minutes": 30},
  {"source": "ticketmaster", "lat": 6.5244, "lng": 3.3792, "radius_km": 50, "interval_minutes": 180},
  {"source": "coverage",   "interval_minutes": 120}
]'
```

Run it as its own service, **or** set `SCHEDULER_IN_PROCESS=true` to run it inside the
API — not both, or every source is ingested twice.

---

## Webhooks

Set `WEBHOOK_URLS` (comma-separated). A POST fires once per event, when its confidence
first reaches `CONFIRMED_CONFIDENCE_THRESHOLD`:

```json
{"event":"event.confirmed","event_id":42,"title":"Lagos Web3 Summit","status":"upcoming",
 "confidence_score":0.87,"category":"web3","start_time":"2026-06-15T18:00:00+00:00",
 "city":"Lagos","evidence_count":3}
```

With `WEBHOOK_SECRET` set, the body is signed: `X-Signature: sha256=<hex HMAC-SHA256>`.

---

## Deployment

See `DEPLOY.md` (Railway + Supabase).

---

## Known limits

- Live global coverage is only as good as the geo-capable sources you have keys
  for. Ticketmaster covers ticketed mainstream events in ~30 countries; informal,
  community and Web3 events in Lagos, Nairobi or Accra still come from per-city
  feeds (Luma ICS, Telegram channels) that someone has to configure. Meetup,
  Facebook Events and Dice have no open geo API.
- LinkedIn and X have no turnkey open ingestion; both rely on feeds / bridges or
  credentials you supply.
- Nominatim's public instance allows 1 req/s; a busy deployment should point
  `GEOCODER_BASE_URL` at a self-hosted Nominatim or Photon.
- Geo normalisation is lightweight: venues are matched by fuzzy name within a city.
  A venue knowledge graph would improve clustering further.
- "Ongoing" detection is confidence-based inference, not ground truth.
- The frontend's RSVP / ticketing / sign-in flows are UI prototypes with no backend.
