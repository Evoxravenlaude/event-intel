from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from app.db.session import get_db
from app.models.event import Event, Venue
from app.schemas.event import EventCreate, EventOut, PaginatedEventsOut
from app.services.event_service import create_event
from app.services.geo import haversine_km, radius_events_query

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventOut)
def post_event(payload: EventCreate, db: Session = Depends(get_db)):
    return create_event(db, payload)


@router.get("", response_model=PaginatedEventsOut)
def list_events(
    db: Session = Depends(get_db),
    category: str | None = None,
    status: str | None = None,
    city: str | None = None,
    lat: float | None = Query(default=None),
    lng: float | None = Query(default=None),
    radius_km: float = Query(default=10.0, gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    q = (
        select(Event)
        .options(
            selectinload(Event.venue),
            selectinload(Event.organizer),
            selectinload(Event.evidence),
        )
        .order_by(Event.start_time.asc().nullslast())
    )
    if category:
        q = q.where(Event.category == category)
    if status:
        q = q.where(Event.status == status)
    if city:
        q = q.join(Event.venue).where(Venue.city.ilike(city))

    # Radius filter — PostGIS when available, Python otherwise
    postgis_used = False
    if lat is not None and lng is not None:
        # Ensure venues are joined for the spatial filter (no-op if already joined)
        if city is None:
            q = q.outerjoin(Event.venue)
        q, postgis_used = radius_events_query(db, q, lat, lng, radius_km)

    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    events = list(
        db.execute(q.offset((page - 1) * page_size).limit(page_size)).scalars().all()
    )

    # Python-side Haversine pass only when PostGIS wasn't used
    if lat is not None and lng is not None and not postgis_used:
        events = [
            e for e in events
            if e.venue
            and e.venue.latitude is not None
            and e.venue.longitude is not None
            and haversine_km(lat, lng, e.venue.latitude, e.venue.longitude) <= radius_km
        ]
        total = len(events)

    return PaginatedEventsOut(total=total, page=page, page_size=page_size, items=events)


@router.get("/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.execute(
        select(Event)
        .options(
            selectinload(Event.venue),
            selectinload(Event.organizer),
            selectinload(Event.evidence),
        )
        .where(Event.id == event_id)
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event
