"""
Aggregate numbers for the dashboard: /stats

Replaces the hard-coded demo counters in the frontend sidebar.
"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.event import Event, RawSignal, ReviewQueueItem, SourceRun, Venue
from app.schemas.event import CountBucket, StatsOut

router = APIRouter(prefix="/stats", tags=["meta"])


@router.get("", response_model=StatsOut)
def get_stats(db: Session = Depends(get_db), top: int = Query(default=10, ge=1, le=50)):
    now = datetime.now(timezone.utc)
    threshold = settings.confirmed_confidence_threshold

    events_total = db.execute(select(func.count()).select_from(Event)).scalar_one()
    events_confirmed = db.execute(
        select(func.count()).select_from(Event).where(Event.confidence_score >= threshold)
    ).scalar_one()
    events_upcoming = db.execute(
        select(func.count()).select_from(Event).where(Event.start_time >= now)
    ).scalar_one()
    signals_total = db.execute(select(func.count()).select_from(RawSignal)).scalar_one()
    signals_unprocessed = db.execute(
        select(func.count()).select_from(RawSignal).where(RawSignal.processed.is_(False))
    ).scalar_one()
    review_pending = db.execute(
        select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.status == "pending")
    ).scalar_one()

    by_category = [
        CountBucket(key=k or "general", count=c)
        for k, c in db.execute(
            select(Event.category, func.count()).group_by(Event.category).order_by(func.count().desc()).limit(top)
        ).all()
    ]
    by_city = [
        CountBucket(key=k, count=c)
        for k, c in db.execute(
            select(Venue.city, func.count(Event.id))
            .join(Event, Event.venue_id == Venue.id)
            .where(Venue.city.is_not(None))
            .group_by(Venue.city)
            .order_by(func.count(Event.id).desc())
            .limit(top)
        ).all()
    ]
    by_source = [
        CountBucket(key=k, count=c)
        for k, c in db.execute(
            select(RawSignal.source_type, func.count()).group_by(RawSignal.source_type).order_by(func.count().desc()).limit(top)
        ).all()
    ]
    sources_last_run = {
        src: last
        for src, last in db.execute(
            select(SourceRun.source, func.max(SourceRun.started_at)).group_by(SourceRun.source)
        ).all()
    }

    from app.models.event import CoverageCell
    areas_covered = db.execute(select(func.count()).select_from(CoverageCell).where(CoverageCell.last_scanned_at.is_not(None))).scalar_one()
    from app.services.adapters import ADAPTERS
    from app.services.parsing import CATEGORIES
    return StatsOut(
        categories=CATEGORIES,
        geo_sources={name: a.is_configured() for name, a in ADAPTERS.items() if a.supports_geo},
        areas_covered=areas_covered,
        events_total=events_total,
        events_confirmed=events_confirmed,
        events_upcoming=events_upcoming,
        signals_total=signals_total,
        signals_unprocessed=signals_unprocessed,
        review_pending=review_pending,
        sources_last_run=sources_last_run,
        by_category=by_category,
        by_city=by_city,
        by_source=by_source,
    )
