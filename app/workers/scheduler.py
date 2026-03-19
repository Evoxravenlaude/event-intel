"""
Recurring ingestion + clustering scheduler.

Run as a standalone process:
    python -m app.workers.scheduler

The scheduler reads SCHEDULER_SOURCES from the environment — a JSON array of
ingest job definitions, each with the same shape as the /signals/ingest payload:

    SCHEDULER_SOURCES='[
      {"source": "luma",      "city": "Lagos",   "query": "web3",    "interval_minutes": 60},
      {"source": "eventbrite","city": "Accra",   "query": "tech",    "interval_minutes": 120},
      {"source": "telegram",  "city": "Lagos",   "interval_minutes": 30}
    ]'

If SCHEDULER_SOURCES is not set, the scheduler logs a warning and exits cleanly.

After each ingestion round the scheduler also runs the clustering pass so new
signals are resolved into events without needing a manual POST /signals/cluster.
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
    _last_run: datetime | None = field(default=None, init=False, repr=False)

    def is_due(self, now: datetime) -> bool:
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
    jobs = []
    for defn in definitions:
        try:
            jobs.append(
                ScheduledJob(
                    source=defn["source"],
                    interval_minutes=int(defn.get("interval_minutes", 60)),
                    city=defn.get("city"),
                    query=defn.get("query"),
                    urls=defn.get("urls", []),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed job definition %s: %s", defn, exc)
    return jobs


async def _run_job(job: ScheduledJob) -> None:
    logger.info("Running scheduled ingest: source=%s city=%s query=%s", job.source, job.city, job.query)
    db = SessionLocal()
    try:
        run = create_source_run(db, source=job.source, city=job.city, query=job.query)
        try:
            result = ingest_from_source(job.source, job.city, job.query, job.urls or None)
            created = [create_signal(db, item) for item in result.items]
            finish_source_run(
                db, run,
                status="completed",
                fetched_count=result.fetched_count,
                created_signal_count=len(created),
            )
            logger.info("Ingest completed: %d signals created (run_id=%d)", len(created), run.id)
        except Exception as exc:
            finish_source_run(db, run, status="failed", fetched_count=0, created_signal_count=0, error=str(exc))
            logger.error("Ingest failed for source=%s: %s", job.source, exc)
            return

        # Auto-cluster after each ingestion run
        event_created, linked, queued = cluster_signals(db)
        logger.info(
            "Clustering done: %d events created, %d signals linked, %d queued for review",
            len(event_created), len(linked), len(queued),
        )
    finally:
        db.close()


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
                # Discard from set when done so memory doesn't grow unbounded
                task.add_done_callback(_active_tasks.discard)
        await asyncio.sleep(tick_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_scheduler())


if __name__ == "__main__":
    main()
