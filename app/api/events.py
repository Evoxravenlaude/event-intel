from __future__ import annotations
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.session import get_db
from app.models.event import Event, Venue
from app.schemas.event import EventCreate, EventOut, EventUpdate, PaginatedEventsOut
from app.services.event_service import create_event, update_event
from app.services.geo import haversine_km, radius_events_query

router = APIRouter(prefix="/events", tags=["events"])


def _event_query():
    return select(Event).options(
        selectinload(Event.venue),
        selectinload(Event.organizer),
        selectinload(Event.evidence),
    )


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def post_event(payload: EventCreate, db: Session = Depends(get_db)):
    return create_event(db, payload)


@router.get("", response_model=PaginatedEventsOut)
def list_events(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, max_length=200, description="Free-text search in title / description"),
    category: str | None = None,
    status: str | None = Query(default=None, description="Comma-separated list accepted, e.g. upcoming,ongoing"),
    city: str | None = Query(default=None, description="Matches venue city, name or address (case-insensitive contains)"),
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0, le=500),
    sort: str = Query(default="start_time", pattern="^(start_time|confidence|created)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    if (lat is None) != (lng is None):
        raise HTTPException(status_code=422, detail="lat and lng must be provided together")

    query = _event_query()
    if sort == "confidence":
        query = query.order_by(Event.confidence_score.desc(), Event.id.desc())
    elif sort == "created":
        query = query.order_by(Event.created_at.desc(), Event.id.desc())
    else:
        query = query.order_by(Event.start_time.asc().nullslast(), Event.id.asc())

    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(Event.title.ilike(like), Event.description.ilike(like)))
    if category:
        query = query.where(Event.category == category)
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        query = query.where(Event.status.in_(statuses))
    if min_confidence is not None:
        query = query.where(Event.confidence_score >= min_confidence)
    if starts_after:
        query = query.where(Event.start_time >= starts_after)
    if starts_before:
        query = query.where(Event.start_time <= starts_before)

    needs_venue_join = bool(city) or lat is not None
    if needs_venue_join:
        query = query.join(Event.venue)
    if city:
        like = f"%{city.strip()}%"
        query = query.where(or_(Venue.city.ilike(like), Venue.name.ilike(like), Venue.address.ilike(like)))

    exact = True
    if lat is not None and lng is not None:
        radius = radius_km or settings.default_radius_km
        query, exact = radius_events_query(db, query, lat, lng, radius)

    if exact:
        total = db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
        events = list(db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all())
    else:
        # No PostGIS: the SQL bounding box has already cut the set down; do the
        # exact circle test in Python *before* paginating so totals and pages
        # are correct (previously the filter ran after LIMIT/OFFSET).
        radius = radius_km or settings.default_radius_km
        rows = db.execute(query).scalars().all()
        events = [
            e for e in rows
            if e.venue is not None and e.venue.has_coordinates
            and haversine_km(lat, lng, e.venue.latitude, e.venue.longitude) <= radius
        ]
        total = len(events)
        events = events[(page - 1) * page_size:(page - 1) * page_size + page_size]

    return PaginatedEventsOut(total=total, page=page, page_size=page_size, items=events)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.execute(_event_query().where(Event.id == event_id)).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.patch("/{event_id}", response_model=EventOut)
def patch_event(event_id: int, payload: EventUpdate, db: Session = Depends(get_db)):
    event = db.execute(_event_query().where(Event.id == event_id)).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return update_event(db, event, payload)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event_id: int, db: Session = Depends(get_db)):
    """
    Delete an event. Its evidence rows are removed and the underlying signals
    are returned to the unprocessed pool so the next cluster pass re-evaluates them.
    """
    event = db.execute(_event_query().where(Event.id == event_id)).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    for ev in event.evidence:
        if ev.raw_signal is not None:
            ev.raw_signal.processed = False
    # Detach any review items pointing at this event
    from app.models.event import ReviewQueueItem
    for item in db.execute(select(ReviewQueueItem).where(ReviewQueueItem.candidate_event_id == event.id)).scalars():
        item.candidate_event_id = None
    db.delete(event)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
