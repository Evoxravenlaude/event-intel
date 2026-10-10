# Event Intel — Audit Report (2026-10-10)

Scope: every file in the uploaded `event-intel-main` archive (43 files). Method: full
read of the codebase, reproduction of each suspected defect, fix, regression test,
then an end-to-end run of the real server driven through the frontend in Chromium.

**Headline:** the product could not be deployed or tested as shipped. The Docker
image would not build on the pinned Python version; if it had, the container would
have exited on boot; the test suite failed 29/60; clustering crashed on the default
SQLite setup; the Supabase bootstrap SQL was a syntax error; and three core data
paths (Luma ICS, review queue, radius search) produced wrong results silently.
All of this is fixed. Test suite: **161 passing** (was 31/60). See the addendum at the end for the location-first discovery layer added afterwards.

Severity key — **P0** blocks deploy / corrupts data · **P1** wrong behaviour users see · **P2** quality / hardening

---

## 1. Build & deploy

| # | Sev | Finding | Fix |
|---|---|---|---|
| 1.1 | P0 | `feedparser==6.0.11` depends on `sgmllib3k`, which fails to build on Python 3.13 — the Dockerfile's base image. `docker build` fails. | `feedparser==6.0.14` (vendors sgmllib). |
| 1.2 | P0 | `CMD ["sh", "scripts/bootstrap.sh"]` runs a bash script under dash; `set -o pipefail` is rejected → container exits immediately. Reproduced: `dash: set: Illegal option -o pipefail`. | `CMD ["bash", ...]`, script hardened (`exec`, `RUN_MIGRATIONS`, `--proxy-headers`). |
| 1.3 | P0 | `railway.json` `startCommand` ran uvicorn directly, bypassing `bootstrap.sh` — migrations never ran on deploy. The `services` block is not part of Railway's schema and was ignored. | Start command is `bash scripts/bootstrap.sh`; per-service commands documented in DEPLOY.md. |
| 1.4 | P0 | `supabase/bootstrap.sql` uses `CREATE POLICY IF NOT EXISTS`, which Postgres does not support — the script errors on line 18. | Rewritten with a `DO` block; idempotent. |
| 1.5 | P0 | RLS was enabled on 5 of 9 tables. `organizers`, `event_evidence`, `source_runs`, `alembic_version` were readable *and writable* by anyone with the Supabase anon key via the auto-generated REST API. | RLS + service-role policy on every table. Also repairs PostGIS/pgvector columns if the migration fell back. |
| 1.6 | P1 | No `.dockerignore` — `.git`, tests, local `.db` files, `.env` baked into the image. | Added. |
| 1.7 | P1 | Embedding model warm-up ran synchronously in `lifespan` (torch import + ~120MB download) before `/health` could answer → Railway healthcheck timeouts on cold start. | Warm-up in a daemon thread; `/ready` reports `loading/loaded/disabled`. |
| 1.8 | P2 | "~50MB model" claim hides a ~1GB torch dependency. | `requirements-ml.txt` split; `--build-arg WITH_EMBEDDINGS=false` builds a slim image. |
| 1.9 | P2 | No CI; the suite had rotted to 29 failures without anyone noticing. | GitHub Actions: pytest + alembic up/down/up + docker build + boot smoke test. |
| 1.10 | P2 | Docs referenced Render in a Railway project; README had a mangled sentence about X configuration. | README and DEPLOY rewritten. |

## 2. Data layer

| # | Sev | Finding | Fix |
|---|---|---|---|
| 2.1 | P0 | `DateTime(timezone=True)` on SQLite returns **naive** datetimes. Any comparison with `datetime.now(timezone.utc)` raised `TypeError`. Reproduced: clustering crashed as soon as a timed signal matched a stored event. Masked in tests only because the fixture was broken (3.1). | `UTCDateTime` TypeDecorator: always aware UTC on read and write. Pydantic inputs coerced too (naive = UTC). |
| 2.2 | P1 | No length validation on inputs; Postgres rejects >255-char titles with a 500. Feed titles routinely exceed this. | `Field(max_length=…)` on every string, `truncate()` in adapters. |
| 2.3 | P1 | `ReviewActionRequest.action` and `IngestRequest.source` were free strings. | `Literal` enums → 422 with a clear message. |
| 2.4 | P1 | Review items exposed only IDs; a reviewer could not see what they were comparing. | `ReviewQueueOut` embeds `signal` and `candidate_event`. |
| 2.5 | P2 | `EventOut` had no evidence count; the frontend hard-coded `evidence: 0`. | `evidence_count` on model + schema. |
| 2.6 | P2 | FK columns used in every list/join (`events.venue_id`, `organizer_id`, `review_queue.candidate_event_id`) and sort columns (`ingested_at`, `created_at`) had no indexes. | Migration `0004`. `alembic check` reports no drift. |
| 2.7 | P2 | `IntegrityError` on duplicate signal races → 500. | Upsert catches the race; global handler maps remaining integrity errors to 409. |

## 3. Tests

| # | Sev | Finding | Fix |
|---|---|---|---|
| 3.1 | P0 | `conftest.py` created `sqlite:///:memory:` without `StaticPool`. TestClient runs the app in another thread → different connection → empty database → `no such table`. **29 of 60 tests failed.** | `StaticPool`. |
| 3.2 | P1 | `test_category_inference_swahili` failed for a real reason (4.1). | Fixed root cause. |
| 3.3 | P1 | `test_create_event_full` hard-coded `2026-06-15` and would flip to `ended` once the date passed (it has). | Relative dates. |
| 3.4 | P1 | `test_review_queue_resolve_reject` returned early when no queue item existed — it could never fail. | Replaced by deterministic review tests. |
| — | — | Added 68 tests: timezone round-trip, review loop, each review action, geo coords, zero-coordinate edge, batch limit, thresholds, webhook transition + HMAC, radius pagination, filters, PATCH/DELETE, `/stats`, `/ready`, auth variants, CORS on 401, JSON-LD, ICS, every adapter over `httpx.MockTransport`, URL validation, partial-failure runs. | |

## 4. Ingestion & parsing

| # | Sev | Finding | Fix |
|---|---|---|---|
| 4.1 | P0 | `infer_category` used substring matching. `"ai"` ⊂ **Nairobi** → every Nairobi post was "tech"; `"art"` ⊂ party/start → culture; `"dao"` ⊂ Daoud → web3; `"dev"` ⊂ device. | Whole-word regex with `(?<!\w)…(?!\w)` guards (handles `google i/o`); ties broken by distinct-hit count. |
| 4.2 | P0 | Luma ICS: `external_id = build_external_id(url, …)` returns the *feed URL* for every VEVENT → **every event in a calendar collapsed into one signal** (reproduced). | `UID` per VEVENT, hash fallback. |
| 4.3 | P1 | ICS all-day events have `date` not `datetime` for `dtstart` → pydantic validation error, whole feed fails. | `parse_datetime` accepts `date`. |
| 4.4 | P1 | Eventbrite adapter called `/v3/events/search/`, **retired by Eventbrite in Feb 2020** — 404 for every token. The README advertised it as working. | Organization-events endpoint (`EVENTBRITE_ORGANIZATION_ID`), paginated; public event pages parsed via JSON-LD (no token). |
| 4.5 | P1 | `feedparser.parse(url)` fetched directly: no timeout, no proxy, ignored `SOURCE_TIMEOUT_SECONDS`. A hung feed hung the request. | Fetch via httpx with the configured timeout, then parse bytes. |
| 4.6 | P1 | HTTP errors in one URL aborted the whole ingest and produced a mock item on retry. | Per-URL errors collected; run status `partial`; mocks only when nothing failed. |
| 4.7 | P1 | X search hard-coded `lang:en` in a product built around French / Pidgin / Swahili posts. | Removed; optional `X_LANGUAGES`. Also uses `api.x.com`, first line as title, place expansion for location. |
| 4.8 | P1 | Page scraping took `<title>` + all `<p>` text. Luma / Eventbrite / LinkedIn all embed schema.org `Event` JSON-LD with exact start/end, venue, geo. | `extract_jsonld_events` + `jsonld_event_to_signal` shared by all page-based paths. |
| 4.9 | P2 | SSRF: `urls` accepted any scheme (`file://`, internal hosts). API key gates it, but still. | `http(s)` only, max 25 URLs. (Private-range blocking left as a follow-up.) |
| 4.10 | P2 | Query filter was ignored for feed sources — every entry was ingested regardless of `query`. | Case-insensitive contains filter applied in Feed and Eventbrite API paths. |

## 5. Clustering & review

| # | Sev | Finding | Fix |
|---|---|---|---|
| 5.1 | P0 | A signal sent to the review queue was never marked `processed`. **Every subsequent cluster pass re-scored it and opened another review item** — the queue grew without bound (reproduced: 1 → 2 → 3 items across passes). | Queued signals marked processed; open-review guard; `recluster` explicitly resets. |
| 5.2 | P0 | Events seeded from signals got a venue with only the location *text*; the signal's `latitude/longitude` were dropped. Clustered events were therefore invisible to radius search and never earned the distance boost. | `create_event_from_signal` passes coordinates; linking also back-fills missing venue coords and start time. |
| 5.3 | P1 | `reject` left the signal unprocessed → re-queued forever (same loop as 5.1) and lost the information that it *was* an event. | `reject` seeds a separate event. New `discard` action for noise. |
| 5.4 | P1 | Webhook fired on **every** link/seed above threshold — one subscriber notification per signal for an already-confirmed event. | `fire_if_newly_confirmed(before, after)` fires only on the crossing. Optional HMAC `X-Signature`. |
| 5.5 | P1 | `_geo_similarity` used truthiness: latitude/longitude `0.0` treated as missing. | `is not None` via `has_coordinates`. |
| 5.6 | P1 | Thresholds (0.55 / 0.35) hard-coded. With MiniLM, unrelated texts commonly score 0.3–0.45 cosine, so the review band needs tuning per deployment. | `CLUSTER_LINK_THRESHOLD`, `CLUSTER_REVIEW_THRESHOLD`, validated ordering. |
| 5.7 | P1 | Full-scan candidate path loaded **every** event for **every** signal; one pass processed the entire backlog in one transaction. | ±N-day time-window prefilter; `CLUSTER_BATCH_SIZE`; `?limit=`; `remaining_unprocessed` in the response. |
| 5.8 | P2 | `approve_link` passed `confidence + 0.05` as "source confidence" and ignored organizer reliability. | Shared `link_signal_to_event` rescoring (max of event/signal confidence, organizer score included). |
| 5.9 | P2 | Alias lookup in venue dedup ignored city — "iHub" alias could resolve across Lagos/Nairobi. | Alias join scoped to city; accent-insensitive normalisation; filler words matched on word boundaries. |

## 6. API & app

| # | Sev | Finding | Fix |
|---|---|---|---|
| 6.1 | P0 | Radius search on SQLite applied the Haversine filter **after** `LIMIT/OFFSET`: `total` was wrong and events beyond page 1 of the unfiltered set were never returned. | SQL bounding box (index-backed, both backends) → exact Haversine → paginate. PostGIS path unchanged. |
| 6.2 | P1 | PostGIS detection checked the extension but not the `venues.location` column (absent if migration 0002's block failed) → SQL error on every radius query. | Checks both. |
| 6.3 | P1 | Production "JSON" logs used `%(message)r` → Python repr with single quotes, not JSON. | `JsonFormatter`; request lines carry `request_id, path, method, status_code, duration_ms`. |
| 6.4 | P1 | Scheduler thread started whenever `SCHEDULER_SOURCES` was set — with the documented separate `scheduler` service, every source was ingested twice. | `SCHEDULER_IN_PROCESS` (default false). |
| 6.5 | P1 | Scheduler `_run_job` was `async` but did blocking httpx + DB work on the loop; concurrent jobs serialised; an exception in one job killed the loop. | `asyncio.to_thread`, per-job `_running` guard, exceptions contained. |
| 6.6 | P1 | `ingest` endpoint called `finish_source_run` on a session that may be in a failed state after a DB error → second exception, 500 with no run record. | `rollback()` first; 400 for bad input, 502 for upstream fetch failure, 500 for storage. |
| 6.7 | P1 | CORS middleware was *inside* the auth middleware → 401 responses had no CORS headers; browsers reported an opaque network error instead of "invalid key". OPTIONS preflights were also rejected with 401. | CORS outermost; OPTIONS exempt. |
| 6.8 | P2 | No readiness probe; `/health` returned 200 with the DB down. `/signals/runs` unpaginated. `radius_km` default ignored `DEFAULT_RADIUS_KM`. No PATCH/DELETE. No text search. No stats. | `/ready`, paginated runs, `q=` on events/signals, `PATCH`/`DELETE /events/{id}`, `/stats`, `sort=`, multi-status filter, `min_confidence`, `Authorization: Bearer` accepted, `X-Request-ID`. |
| 6.9 | P2 | `allow_credentials=True` with `allow_origins=["*"]`. | Credentials only with explicit origins. |
| 6.10 | P2 | `openapi.json` exposed in production even with docs hidden. | Gated by `DEBUG`. |

## 7. Frontend (`app/static/index.html`)

| # | Sev | Finding | Fix |
|---|---|---|---|
| 7.1 | P0 | **Stored XSS.** Signal and event titles were interpolated into `innerHTML` unescaped. Titles come from third-party RSS/ICS/HTML. Verified: `<img src=x onerror=…>` executed in the admin's browser before the fix; renders as inert text after. | `esc()` applied to every interpolation of external data. |
| 7.2 | P1 | `#screen-success{display:flex}` outranked `.screen{display:none}` → the RSVP success screen was **permanently rendered** as a translucent overlay on every other screen (visible in baseline screenshots). | Scoped to `#screen-success.active`. |
| 7.3 | P1 | Live mode ignored the selected city; the "Events by City" widget showed hard-coded London/New York counts; hero stats were the literals `247 / 1.2k / 89`. | `city=` passed to the API; sidebar + all counters driven by `/stats`. Demo-mode stats computed from the demo set. |
| 7.4 | P1 | No review-queue UI at all, although it is the product's core human loop. | Review panel in Control: both sides of the match, score, source link, **Same event / Different event / Not an event** buttons, live count. |
| 7.5 | P1 | Demo events leaked into live mode (search pool and detail fallback mixed `MOCK_EVENTS` with live data). | Mocks only when not connected. |
| 7.6 | P2 | Ingest didn't send the city or allow URLs; errors showed a bare "Ingest failed"; a wrong API key silently showed demo data. | City + URLs sent; cluster summary shown; key validated against `/stats` on connect; LOCKED badge when a key is required; key kept in `sessionStorage`. |
| 7.7 | P2 | Evidence count always 0. | Uses `evidence_count`. |

---

## Verified end-to-end

With the real server (`bash scripts/bootstrap.sh`, SQLite, embeddings off) driven by
Playwright/Chromium:

1. Frontend auto-connected (`● LIVE`), feed filtered to Lagos, card shows "2 signals".
2. `/stats` populated five counters and the city widget.
3. A near-match signal appeared in the Review Queue with both sides rendered.
4. "Different event" resolved it: events 2→3, pending 1→0, no re-queue on the next pass.
5. Ingest from the UI with Lagos selected → run #2, 1 signal, 1 to review.
6. Hostile `<img onerror>` title: not executed, rendered as text.
7. `alembic upgrade head → downgrade base → upgrade head` clean; `alembic check` no drift.

## Not done / follow-ups

- **Private-IP blocking** for ingest URLs (SSRF hardening beyond scheme check).
- **Rate limiting** on `/signals/ingest` and `/signals/cluster`.
- **Auth for the frontend** beyond a shared API key (the sign-in UI is a prototype).
- **Postgres integration tests** (PostGIS / pgvector paths are unit-tested with mocks only).
- Frontend **RSVP / ticket / wallet** flows have no backend; they remain UI prototypes.
- A **venue knowledge graph** for geo normalisation, as the original README noted.

---

# Addendum — gap against the product vision (2026-10-10, same day)

**Stated vision:** a global product where you open the app anywhere and scan for
all kinds of events around you.

**Assessment of the audited code against that:** it was a *per-city feed
pipeline*. Events only existed where someone had pre-configured a Luma/Telegram/
RSS feed and a scheduler job for that city; nothing could be asked "what is near
this point?"; most ingested signals carried only a location *string* and could
never appear in a radius search; and the four categories (web3/tech/business/
culture) reflected a niche, not "all kinds of events". The frontend's "Find
events near me" snapped to the nearest of 30 hard-coded cities.

## What was built

| Area | Change |
|---|---|
| **Geocoding** (`services/geocoding.py`, `geocode_cache`) | Nominatim-compatible forward + reverse geocoding, DB-cached, rate-limited, circuit-breaker on failure, ~90-city offline gazetteer fallback. Hooked into venue creation: text → coordinates, coordinates → city/country. `/geocode`, `/geocode/reverse`. |
| **Geo-capable sources** (`adapters.py`) | `GeoQuery(lat, lng, radius_km)` on the adapter contract, `supports_geo` + `is_configured()`. **Ticketmaster Discovery** (30+ countries) and **SeatGeek** adapters; `/signals/ingest` and scheduler jobs accept a point. Upstream base URLs configurable. |
| **Discovery** (`services/discovery.py`, `/discover`) | Reverse-geocode → check scan coverage → run geo sources if the circle isn't freshly covered → cluster → return nearby events + coverage report. Scan *circles* (centre + radius, `coverage_cells`) with containment checks; `refresh=true`, `scan=false`, `/discover/scan`, `/discover/coverage`. Scheduler pseudo-source `"coverage"` re-scans areas requested in the last N days. |
| **Categories** | +sports, food, nightlife, wellness, community, education with multilingual keywords; source taxonomies (Ticketmaster segments, SeatGeek types) mapped directly; phrase-weighted tie-break ("street food" beats "festival"). |
| **Frontend** | Real geolocation → `/discover`; city search hits `/geocode` for any place on Earth; radius slider (persisted); coverage line ("Nairobi, Kenya · scanned 4 min ago via ticketmaster"); area-scoped hero counters; map centred on you with a "you are here" marker; all categories as chips; source-status pills and "areas scanned" in Control. |
| **Schema** | Migration 0005: `geocode_cache`, `coverage_cells`, `venues.country_code`, `venues.geo_source`. |

## Bugs the new flow exposed (and fixed)

| Sev | Finding | Fix |
|---|---|---|
| P0 | **Identical titles in different cities were merged.** "Jazz at the Terrace" in Kigali linked to the Nairobi event 1,000 km away — distance was only ever a bonus. | Hard vetoes: both geolocated and > 50 km apart, or both timed and > 3 days apart → score 0 (`CLUSTER_MAX_LINK_DISTANCE_KM`, `CLUSTER_MAX_LINK_TIME_DAYS`). |
| P0 | **Venue dedup merged same-named venues across cities** ("Impact Hub", "Hard Rock Cafe") when the signal had coordinates but no city string. | City resolved from coordinates *before* matching; matches with coordinates > 10 km apart rejected. |
| P1 | Two different events at neighbouring venues with the same category scored 0.35 (distance + category) and hit the review queue — every busy venue would flood it. | Boosts only count when there is title similarity ≥ 0.1 or a time match. |
| P1 | Category ties fell to dict order: "Street Food Festival" → culture. | Phrase-weighted scoring. |
| P1 | A single verified ticketed listing capped at 0.73 confidence — just under "confirmed". | Structured sources (ticketing, calendars) get 0.9 geo/time precision when they supply exact coordinates/times. |
| P2 | First coverage model (fixed 5 km geohash cells) exploded to 625 rows per 80 km scan and mis-judged freshness at coarse precision. | Replaced with scan circles + containment before shipping. |

## Verified end-to-end (Chromium, real server, mock Ticketmaster upstream)

1. Granted geolocation in Nairobi → "Near you · Nairobi", scan via ticketmaster, 6 events (culture/sports/food), coverage line "scanned 0s ago · 6 new".
2. Reload → "fresh", no scan, same events.
3. City search "Kigali" → geocoded (offline gazetteer), own scan, 6 *separate* events; Nairobi's untouched.
4. Radius 25 → 80 km re-queries and reports.
5. `/stats`: 2 areas scanned, 12 events, 12 confirmed. Zero JS errors.

## Still open

- **Source breadth.** Ticketmaster + SeatGeek cover ticketed mainstream events. Informal/community/Web3 events in African cities still depend on configured Luma/Telegram feeds. Candidates with usable APIs: Bandsintown (artist-centric), Eventbrite org feeds, PredictHQ (paid), Meetup (OAuth, GraphQL). Facebook Events and Dice have no open API.
- **Async scans.** `/discover` scans synchronously (bounded ~25 s). At scale, enqueue the scan and return cached results immediately with `coverage.reason = "scanning"`, then poll.
- **Nominatim rate limit** (1 req/s on the public instance) — self-host or use Photon for volume.
- **Timezones.** Event times are stored UTC and displayed in the browser's zone, which equals the venue's zone for "near me" but not for a remote city search. A `timezone` column (from the geocoder) would fix the remote case.
- The RSVP / ticket / sign-in UI remains a prototype with no backend.
