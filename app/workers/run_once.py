"""
One-shot worker.

    python -m app.workers.run_once                 # cluster pass only
    python -m app.workers.run_once --ingest        # run every SCHEDULER_SOURCES job once, then cluster
    python -m app.workers.run_once --loop          # keep clustering until no unprocessed signals remain

Exit code 0 on success, 1 on error (so Railway marks the run as failed).
"""
from __future__ import annotations
import argparse
import json
import logging
import sys

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services.clustering import cluster_signals, count_unprocessed

logger = logging.getLogger("run_once")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Event Intel one-shot worker")
    parser.add_argument("--ingest", action="store_true", help="run all SCHEDULER_SOURCES jobs once before clustering")
    parser.add_argument("--loop", action="store_true", help="repeat cluster passes until the backlog is empty")
    args = parser.parse_args(argv)

    configure_logging(production=settings.app_env == "production")

    if args.ingest:
        from app.workers.scheduler import _load_jobs, run_ingest_job
        jobs = _load_jobs()
        if not jobs:
            logger.warning("--ingest given but SCHEDULER_SOURCES has no jobs")
        for job in jobs:
            summary = run_ingest_job(job.source, job.city, job.query, job.urls, job.lat, job.lng, job.radius_km)
            print(json.dumps({"job": job.source, **summary}))

    db = SessionLocal()
    try:
        totals = {"created": 0, "linked": 0, "queued": 0}
        while True:
            created, linked, queued = cluster_signals(db)
            totals["created"] += len(created)
            totals["linked"] += len(linked)
            totals["queued"] += len(queued)
            remaining = count_unprocessed(db)
            if not args.loop or remaining == 0 or not (created or linked or queued):
                break
        print(json.dumps({**totals, "remaining_unprocessed": remaining}))
        return 0
    except Exception:
        logger.exception("Cluster pass failed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
