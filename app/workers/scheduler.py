"""
Recurring ingestion + clustering scheduler.

Run as a standalone process:
    python -m app.workers.scheduler

or inside the API process with SCHEDULER_IN_PROCESS=true (single-service
deploys only — running both doubles every ingest).

SCHEDULER_SOURCES is a JSON array of ingest job definitions, each with the
same shape as the /signals/ingest payload plus `interval_minutes`:

    SCHEDULER_SOURCES='[
      {"source": "luma",      "city": "Lagos",   "query": "web3",    "interval_minutes": 60},
      {"source": "eventbrite","city": "Accra",   "query": "tech",    "interval_minutes": 120},
      {"source": "telegram",  "city": "Lagos",   "interval_minutes": 30},
      {"source": "ticketmaster", "lat": 6.5244, "lng": 3.3792, "radius_km": 50, "interval_minutes": 180},
      {"source": "coverage",  "interval_minutes": 120}
    ]'

If SCHEDULER_SOURCES is not set, the scheduler logs a warning and exits cleanly.

After each ingestion round the scheduler also runs the clustering pass so new
signals are resolved into events without needing a manual POST /signals/cluster.
Ingest is blocking I/O (httpx, SQLAlchemy) so each job runs in a worker thread;
jobs that are due at the same tick run concurrently.
"""
from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.adapters import ingest_from_source
from app.services.clustering import cluster_signals
from app.services.event_service import create_signal, create_source_run, finish_source_run

logger = logging.getLogger(__name__)


@dataclass
class ScheduledJob:
    source: str
    interval_minutes: int
    city: str | None = None
    query: str | None = None
    urls: list[str] = field(default_factory=list)
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    _last_run: datetime | None = field(default=None, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)

    def is_due(self, now: datetime) -> bool:
        if self._running:
            return False
        if self._last_run is None:
            return True
        elapsed = (now - self._last_run).total_seconds() / 60
        return elapsed >= self.interval_minutes

    def mark_run(self, now: datetime) -> None:
        self._last_run = now


def _load_jobs() -> list[ScheduledJob]:
    raw = settings.scheduler_sources or ""
    if not raw:
        logger.warning("SCHEDULER_SOURCES not set — scheduler has no jobs to run.")
        return []
    try:
        definitions = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse SCHEDULER_SOURCES: %s", exc)
        return []
    if not isinstance(definitions, list):
        logger.error("SCHEDULER_SOURCES must be a JSON array")
        return []
    jobs = []
    for defn in definitions:
        try:
            interval = int(defn.get("interval_minutes", 60))
            if interval < 1:
                raise ValueError("interval_minutes must be >= 1")
            jobs.append(
                ScheduledJob(
                    source=defn["source"],
                    interval_minutes=interval,
                    city=defn.get("city"),
                    query=defn.get("query"),
                    urls=list(defn.get("urls", []) or []),
                    lat=float(defn["lat"]) if defn.get("lat") is not None else None,
                    lng=float(defn["lng"]) if defn.get("lng") is not None else None,
                    radius_km=float(defn["radius_km"]) if defn.get("radius_km") is not None else None,
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Skipping malformed job definition %s: %s", defn, exc)
    return jobs


def run_ingest_job(
    source: str, city: str | None, query: str | None, urls: list[str] | None,
    lat: float | None = None, lng: float | None = None, radius_km: float | None = None,
) -> dict:
    """
    Synchronous ingest + cluster for one job. Shared by the scheduler and
    the one-shot worker. Returns a small summary dict.

    The pseudo-source "coverage" re-scans every area that was requested via
    /discover in the last COVERAGE_RESCAN_DAYS, keeping places people
    actually use fresh without anyone configuring them.
    """
    if source == "coverage":
        from app.services.discovery import rescan_recent_cells
        db = SessionLocal()
        try:
            summary = rescan_recent_cells(db)
            logger.info("Coverage rescan done: %d area(s), %d signal(s)", summary.get("areas", 0), summary.get("signals", 0))
            return summary
        finally:
            db.close()

    from app.services.adapters import GeoQuery
    geo = GeoQuery(lat=lat, lng=lng, radius_km=radius_km or settings.discover_default_radius_km) if lat is not None and lng is not None else None
    db = SessionLocal()
    try:
        run = create_source_run(db, source=source, city=city, query=query)
        try:
            result = ingest_from_source(source, city, query, urls or None, geo=geo)
            created = [create_signal(db, item) for item in result.items]
        except Exception as exc:
            db.rollback()
            finish_source_run(db, run, status="failed", fetched_count=0, created_signal_count=0, error=str(exc))
            logger.error("Ingest failed for source=%s: %s", source, exc)
            return {"run_id": run.id, "status": "failed", "error": str(exc)}

        run_status = "completed" if not result.errors else ("partial" if created else "failed")
        finish_source_run(
            db, run, status=run_status, fetched_count=result.fetched_count,
            created_signal_count=len(created), error="; ".join(result.errors) or None,
        )
        logger.info("Ingest %s: %d signals created (run_id=%d)", run_status, len(created), run.id)

        new_events, linked, queued = cluster_signals(db)
        logger.info(
            "Clustering done: %d events created, %d signals linked, %d queued for review",
            len(new_events), len(linked), len(queued),
        )
        return {
            "run_id": run.id, "status": run_status, "signals": len(created),
            "events_created": len(new_events), "linked": len(linked), "queued": len(queued),
        }
    finally:
        db.close()


async def _run_job(job: ScheduledJob) -> None:
    job._running = True
    try:
        logger.info("Running scheduled ingest: source=%s city=%s query=%s", job.source, job.city, job.query)
        await asyncio.to_thread(run_ingest_job, job.source, job.city, job.query, job.urls, job.lat, job.lng, job.radius_km)
    except Exception as exc:  # never let one job kill the loop
        logger.exception("Scheduled job crashed: %s", exc)
    finally:
        job._running = False


async def run_scheduler(tick_seconds: int = 60) -> None:
    """Main scheduler loop. Checks all jobs every `tick_seconds` seconds."""
    jobs = _load_jobs()
    if not jobs:
        logger.info("No scheduler jobs found. Exiting.")
        return

    logger.info("Scheduler started with %d job(s). Tick interval: %ds", len(jobs), tick_seconds)
    # Strong reference set — prevents tasks from being GC'd before completion
    _active_tasks: set[asyncio.Task] = set()

    while True:
        now = datetime.now(timezone.utc)
        for job in jobs:
            if job.is_due(now):
                job.mark_run(now)
                task = asyncio.create_task(_run_job(job))
                _active_tasks.add(task)
                task.add_done_callback(_active_tasks.discard)
        await asyncio.sleep(tick_seconds)


def main() -> None:
    from app.core.logging import configure_logging
    configure_logging(production=settings.app_env == "production")
    asyncio.run(run_scheduler(tick_seconds=settings.scheduler_tick_seconds))


if __name__ == "__main__":
    main()
