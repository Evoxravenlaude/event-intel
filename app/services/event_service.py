from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.event import Event, Organizer, RawSignal, SourceRun, Venue
from app.schemas.event import EventCreate, RawSignalCreate
from app.services.scoring import infer_status, score_event


from app.services.venue_dedup import find_fuzzy_venue, record_alias
from app.services import embeddings as emb_svc


def get_or_create_venue(
    db: Session,
    *,
    name: str,
    city: str | None = None,
    country: str | None = None,
    address: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> Venue:
    # 1. Exact match (fast path)
    q = select(Venue).where(Venue.name == name)
    if city:
        q = q.where(Venue.city == city)
    venue = db.execute(q).scalar_one_or_none()

    if not venue:
        # 2. Fuzzy match — catches name variants like "Landmark Event Centre" vs "Landmark Events Centre"
        venue = find_fuzzy_venue(db, name, city)
        if venue:
            # Store the incoming variant as an alias for future fast lookups
            record_alias(db, venue, name)
            db.flush()

    if venue:
        if latitude is not None and venue.latitude is None:
            venue.latitude = latitude
        if longitude is not None and venue.longitude is None:
            venue.longitude = longitude
        return venue

    # 3. Create new venue
    venue = Venue(name=name, city=city, country=country, address=address, latitude=latitude, longitude=longitude)
    db.add(venue)
    db.flush()
    return venue


def get_or_create_organizer(db: Session, organizer_name: str | None) -> Organizer | None:
    if not organizer_name:
        return None
    organizer = db.execute(select(Organizer).where(Organizer.name == organizer_name)).scalar_one_or_none()
    if organizer:
        return organizer
    organizer = Organizer(name=organizer_name)
    db.add(organizer)
    db.flush()
    return organizer


def create_event(db: Session, payload: EventCreate) -> Event:
    venue = None
    if payload.venue:
        venue = get_or_create_venue(db, **payload.venue.model_dump())

    organizer = get_or_create_organizer(db, payload.organizer_name)
    geo_score = 0.9 if payload.venue and payload.venue.latitude is not None and payload.venue.longitude is not None else 0.4
    time_score = 0.9 if payload.start_time else 0.2
    organizer_score = organizer.reliability_score if organizer else 0.0
    event = Event(
        title=payload.title,
        description=payload.description,
        category=payload.category,
        structure_type=payload.structure_type,
        start_time=payload.start_time,
        end_time=payload.end_time,
        venue_id=venue.id if venue else None,
        organizer_id=organizer.id if organizer else None,
        geo_precision_score=geo_score,
        time_precision_score=time_score,
    )
    event.confidence_score = score_event(0.6, 1, geo_score, time_score, organizer_score)
    event.status = infer_status(event.start_time, event.end_time, event.confidence_score)
    db.add(event)
    db.flush()
    emb_svc.embed_event(db, event)
    db.commit()
    db.refresh(event)
    return event


def create_signal(db: Session, payload: RawSignalCreate) -> RawSignal:
    existing = None
    if payload.external_id:
        existing = db.execute(
            select(RawSignal).where(
                RawSignal.source_type == payload.source_type,
                RawSignal.external_id == payload.external_id,
            )
        ).scalar_one_or_none()
    if existing:
        for field, value in payload.model_dump().items():
            if value is not None:
                setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        return existing
    signal = RawSignal(**payload.model_dump())
    db.add(signal)
    db.flush()
    emb_svc.embed_signal(db, signal)
    db.commit()
    db.refresh(signal)
    return signal


def create_source_run(db: Session, *, source: str, city: str | None, query: str | None) -> SourceRun:
    run = SourceRun(source=source, city=city, query=query)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_source_run(db: Session, run: SourceRun, *, status: str, fetched_count: int, created_signal_count: int, error: str | None = None) -> SourceRun:
    run.status = status
    run.fetched_count = fetched_count
    run.created_signal_count = created_signal_count
    run.error = error
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
