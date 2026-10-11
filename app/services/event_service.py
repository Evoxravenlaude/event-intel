from __future__ import annotations
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.event import Event, EventEvidence, Organizer, RawSignal, SourceRun, Venue
from app.schemas.event import EventCreate, EventUpdate, RawSignalCreate
from app.services import embeddings as emb_svc
from app.services.parsing import infer_structure_type
from app.services.scoring import infer_status, score_event
from app.services.geo import haversine_km
from app.services.venue_dedup import find_fuzzy_venue, record_alias

# Two venues with the same name but coordinates further apart than this are different places.
VENUE_MAX_MATCH_KM = 10.0

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Venues / organizers
# ---------------------------------------------------------------------------

def get_or_create_venue(
    db: Session,
    *,
    name: str,
    city: str | None = None,
    country: str | None = None,
    address: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    country_code: str | None = None,
    geocode: bool = True,
) -> Venue:
    """
    Find or create a venue. When no coordinates are supplied and `geocode` is
    on, the name (+ city hint) is forward-geocoded so the venue — and every
    event at it — becomes reachable by radius search. When coordinates ARE
    supplied but no city is, they are reverse-geocoded so the venue gets a
    real city/country label for filtering and stats.
    """
    has_point = latitude is not None and longitude is not None

    # 0. Know the city before matching: "Impact Hub" exists in Accra, Nairobi
    #    and Kigali, so matching must be city-scoped. Cheap — cached.
    if has_point and not city and geocode:
        from app.services import geocoding
        try:
            place = geocoding.reverse(db, latitude, longitude)
            if place:
                city = place.city or city
                country = country or place.country
                country_code = country_code or place.country_code
        except Exception as exc:
            logger.warning("Reverse geocode for venue %r failed: %s", name, exc)

    def _too_far(v: Venue) -> bool:
        return has_point and v.has_coordinates and haversine_km(latitude, longitude, v.latitude, v.longitude) > VENUE_MAX_MATCH_KM

    # 1. Exact match (fast path)
    q = select(Venue).where(Venue.name == name)
    if city:
        q = q.where(Venue.city.ilike(city))
    venue = next((v for v in db.execute(q).scalars().all() if not _too_far(v)), None)

    if not venue:
        # 2. Fuzzy match — catches name variants like "Landmark Event Centre" vs "Landmark Events Centre"
        candidate = find_fuzzy_venue(db, name, city)
        if candidate and not _too_far(candidate):
            venue = candidate
            record_alias(db, venue, name)
            db.flush()

    if venue:
        # Enrich the existing record with anything we didn't have before
        if has_point and not venue.has_coordinates:
            venue.latitude, venue.longitude = latitude, longitude
            venue.geo_source = "source"
        if address and not venue.address:
            venue.address = address
        if country and not venue.country:
            venue.country = country
        if country_code and not venue.country_code:
            venue.country_code = country_code
        if city and not venue.city:
            venue.city = city
        if geocode:
            _enrich_venue_geo(db, venue)
        return venue

    # 3. Create new venue
    venue = Venue(
        name=name, city=city, country=country, country_code=country_code, address=address,
        latitude=latitude, longitude=longitude,
        geo_source="source" if has_point else None,
    )
    db.add(venue)
    db.flush()
    if geocode:
        _enrich_venue_geo(db, venue)
    return venue


def split_venue_text(text: str) -> tuple[str, str | None]:
    """
    "Ilé MIMU, 1 Adeola Odeku St, Victoria Island, Lagos 106104, Lagos, Nigeria"
      → ("Ilé MIMU", <full string as address>)
    "Landmark Centre, Lagos" → ("Landmark Centre", "Landmark Centre, Lagos")
    "Lagos" → ("Lagos", None)
    The full string is kept as the address because it geocodes far better
    than the venue name alone.
    """
    text = text.strip()
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        return text, None
    return parts[0][:255], text[:500]


def _enrich_venue_geo(db: Session, venue: Venue) -> None:
    """Fill coordinates from text, or city/country from coordinates. Never raises."""
    from app.services import geocoding
    try:
        if not venue.has_coordinates:
            query = venue.address or venue.name
            place = geocoding.forward(db, query, city_hint=venue.city if venue.city and venue.city != venue.name else None)
            if place is None and venue.city and venue.city != venue.name:
                place = geocoding.forward(db, venue.city)
            if place:
                venue.latitude, venue.longitude = place.latitude, place.longitude
                venue.geo_source = place.source if place.source != "cache" else "geocoder"
                venue.city = venue.city if (venue.city and venue.city != venue.name) else (place.city or venue.city)
                venue.country = venue.country or place.country
                venue.country_code = venue.country_code or place.country_code
        elif not venue.city or not venue.country_code:
            place = geocoding.reverse(db, venue.latitude, venue.longitude)
            if place:
                venue.city = venue.city or place.city
                venue.country = venue.country or place.country
                venue.country_code = venue.country_code or place.country_code
        db.flush()
    except Exception as exc:  # geocoding must never break ingestion
        logger.warning("Venue geo enrichment failed for %r: %s", venue.name, exc)


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


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def create_event(db: Session, payload: EventCreate) -> Event:
    venue = None
    if payload.venue:
        venue = get_or_create_venue(db, **payload.venue.model_dump())

    organizer = get_or_create_organizer(db, payload.organizer_name)
    geo_score = 0.9 if venue and venue.has_coordinates else (0.5 if venue else 0.3)
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


def update_event(db: Session, event: Event, payload: EventUpdate) -> Event:
    data = payload.model_dump(exclude_unset=True)
    venue_in = data.pop("venue", None)
    organizer_name = data.pop("organizer_name", None)
    explicit_status = data.pop("status", None)

    for field, value in data.items():
        setattr(event, field, value)

    if venue_in:
        venue = get_or_create_venue(db, **venue_in)
        event.venue_id = venue.id
        event.geo_precision_score = 0.9 if venue.has_coordinates else 0.5
    if organizer_name is not None:
        organizer = get_or_create_organizer(db, organizer_name)
        event.organizer_id = organizer.id if organizer else None
    if "start_time" in data:
        event.time_precision_score = 0.9 if event.start_time else 0.2

    if "title" in data or "description" in data:
        emb_svc.embed_event(db, event)

    event.status = explicit_status or infer_status(event.start_time, event.end_time, event.confidence_score)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def create_event_from_signal(db: Session, signal: RawSignal, *, evidence_type: str = "seed_signal") -> Event:
    """
    Seed a brand-new event from a single signal, link the signal as evidence
    and mark it processed. Used by clustering (no match) and by review
    ("reject" = this is a different event).

    The venue inherits the signal's coordinates so the event is reachable
    via radius search — previously only the location text was copied.
    """
    venue = None
    if signal.location_text:
        # location_text is whatever the source gave us: for feed/X sources it is
        # the ingest city, for ICS it is a venue string ("Landmark Centre, Lagos").
        # get_or_create_venue geocodes it: coordinates from the text, and the
        # real city/country from the coordinates (gazetteer fallback offline).
        name, address = split_venue_text(signal.location_text)
        venue = get_or_create_venue(
            db,
            name=name,
            address=address,
            city=None,
            latitude=signal.latitude if signal.has_coordinates else None,
            longitude=signal.longitude if signal.has_coordinates else None,
        )
    structure = infer_structure_type(signal.source_type)
    # Structured sources (ticketing platforms, calendars) give exact venue
    # coordinates and start times; social posts give "tonight at Yaba".
    exact = structure == "structured"
    event = Event(
        title=signal.title or "Untitled event",
        description=signal.body,
        category=signal.normalized_category or "general",
        structure_type=structure,
        geo_precision_score=(0.9 if exact else 0.75) if signal.has_coordinates else (0.55 if venue and venue.has_coordinates else (0.35 if venue else 0.2)),
        time_precision_score=(0.9 if exact else 0.8) if signal.detected_start_time else 0.2,
        start_time=signal.detected_start_time,
        end_time=signal.detected_end_time,
        venue_id=venue.id if venue else None,
    )
    event.confidence_score = score_event(
        signal.source_confidence, 1, event.geo_precision_score, event.time_precision_score,
    )
    event.status = infer_status(event.start_time, event.end_time, event.confidence_score)
    db.add(event)
    db.flush()
    if signal.embedding is not None:
        event.embedding = signal.embedding  # same text → reuse, saves an encode
    else:
        emb_svc.embed_event(db, event)
    db.add(EventEvidence(
        event_id=event.id,
        raw_signal_id=signal.id,
        weight=signal.source_confidence,
        evidence_type=evidence_type,
    ))
    signal.processed = True
    db.flush()
    return event


def link_signal_to_event(
    db: Session, signal: RawSignal, event: Event, *, weight: float, evidence_type: str,
) -> tuple[float, float]:
    """
    Attach `signal` as evidence of `event`, rescore the event and mark the
    signal processed. Returns (confidence_before, confidence_after) so the
    caller can decide whether a "confirmed" webhook should fire.
    """
    before = event.confidence_score
    db.add(EventEvidence(
        event_id=event.id,
        raw_signal_id=signal.id,
        weight=min(max(weight, 0.0), 1.0),
        evidence_type=evidence_type,
    ))
    evidence_count = event.evidence_count + 1
    # Blend the event's running confidence with this signal's own confidence
    source_conf = max(event.confidence_score, signal.source_confidence)
    event.confidence_score = score_event(
        source_conf, evidence_count, event.geo_precision_score, event.time_precision_score,
        event.organizer.reliability_score if event.organizer else 0.0,
    )
    # Fill in anything the event is missing from the new signal
    if event.start_time is None and signal.detected_start_time is not None:
        event.start_time = signal.detected_start_time
        event.end_time = event.end_time or signal.detected_end_time
        event.time_precision_score = max(event.time_precision_score, 0.6)
    if event.venue is not None and not event.venue.has_coordinates and signal.has_coordinates:
        event.venue.latitude, event.venue.longitude = signal.latitude, signal.longitude
        event.geo_precision_score = max(event.geo_precision_score, 0.7)
    event.status = infer_status(event.start_time, event.end_time, event.confidence_score)
    signal.processed = True
    db.add(event)
    db.flush()
    return before, event.confidence_score


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _find_existing_signal(db: Session, payload: RawSignalCreate) -> RawSignal | None:
    if not payload.external_id:
        return None
    return db.execute(
        select(RawSignal).where(
            RawSignal.source_type == payload.source_type,
            RawSignal.external_id == payload.external_id,
        )
    ).scalar_one_or_none()


def create_signal(db: Session, payload: RawSignalCreate) -> RawSignal:
    """
    Insert a signal, or update the existing one with the same
    (source_type, external_id). Re-ingested signals whose text changed get a
    fresh embedding and are re-queued for clustering.
    """
    existing = _find_existing_signal(db, payload)
    if existing:
        return _update_signal(db, existing, payload)

    signal = RawSignal(**payload.model_dump())
    db.add(signal)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with a concurrent ingest of the same item — update instead
        db.rollback()
        existing = _find_existing_signal(db, payload)
        if existing is None:
            raise
        return _update_signal(db, existing, payload)
    emb_svc.embed_signal(db, signal)
    db.commit()
    db.refresh(signal)
    return signal


def _update_signal(db: Session, existing: RawSignal, payload: RawSignalCreate) -> RawSignal:
    text_before = (existing.title, existing.body)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(existing, field, value)
    if (existing.title, existing.body) != text_before:
        emb_svc.embed_signal(db, existing)
        # Content changed: let the next cluster pass re-evaluate it, unless it
        # is already attached to an event (evidence stays authoritative).
        has_evidence = db.execute(
            select(EventEvidence.id).where(EventEvidence.raw_signal_id == existing.id).limit(1)
        ).scalar_one_or_none()
        if has_evidence is None:
            existing.processed = False
    db.commit()
    db.refresh(existing)
    return existing


# ---------------------------------------------------------------------------
# Source runs
# ---------------------------------------------------------------------------

def create_source_run(db: Session, *, source: str, city: str | None, query: str | None) -> SourceRun:
    run = SourceRun(source=source, city=city, query=query)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def finish_source_run(
    db: Session,
    run: SourceRun,
    *,
    status: str,
    fetched_count: int,
    created_signal_count: int,
    error: str | None = None,
) -> SourceRun:
    run.status = status
    run.fetched_count = fetched_count
    run.created_signal_count = created_signal_count
    run.error = error[:4000] if error else None
    run.finished_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
