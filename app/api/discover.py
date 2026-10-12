"""
/discover — the location-first entry point for the app.

GET  /discover?lat&lng&radius_km&q&category&refresh   events near a point; scans the area first if it's stale
POST /discover/scan {lat,lng,radius_km,q}              force a scan, return the report only
GET  /discover/coverage                                 recently scanned areas (for the map / ops)
GET  /geocode?q=                                        free-text place → coordinates (city search)
GET  /geocode/reverse?lat&lng                           coordinates → city / country
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.session import get_db
from app.models.event import CoverageCell, Event, EventEvidence, Venue
from app.schemas.event import EventOut
from app.services import geocoding
from app.services.discovery import scan_area
from app.services.geo import haversine_km, radius_events_query

router = APIRouter(tags=["discover"])


class PlaceOut(BaseModel):
    latitude: float
    longitude: float
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    display_name: str | None = None
    source: str
    neighbourhood: str | None = None
    locality: str | None = None
    accuracy: str | None = None
    label: str | None = None


class DiscoverEventOut(EventOut):
    distance_km: float | None = None


class CoverageOut(BaseModel):
    scanned: bool
    reason: str
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    sources_run: list[str] = []
    sources_skipped: list[str] = []
    signals_created: int = 0
    events_created: int = 0
    signals_linked: int = 0
    queued_for_review: int = 0
    errors: list[str] = []
    last_scanned_at: datetime | None = None
    cells: int = 0


class DiscoverOut(BaseModel):
    center: PlaceOut
    radius_km: float
    total: int
    page: int
    page_size: int
    items: list[DiscoverEventOut]
    coverage: CoverageOut
    sort: str = "time"


class ScanRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0)
    q: str | None = Field(default=None, max_length=255)


class CoverageCellOut(BaseModel):
    cell: str
    latitude: float
    longitude: float
    radius_km: float
    city: str | None = None
    country_code: str | None = None
    scan_count: int
    last_signal_count: int
    last_scanned_at: datetime | None = None
    last_requested_at: datetime | None = None

    model_config = {"from_attributes": True}


MAX_CANDIDATES = 5000


def _nearby_events(
    db: Session, lat: float, lng: float, radius_km: float, *, q: str | None, category: str | None,
    include_past: bool, sort: str, page: int, page_size: int,
) -> tuple[int, list[tuple[Event, float]]]:
    """
    Events within `radius_km` of the point, each with its exact distance.
    SQL narrows to the bounding box (PostGIS: the exact circle); the precise
    great-circle filter, sort and pagination happen here so distance sorting
    and per-event distances are always correct.
    """
    query = (
        select(Event)
        .options(selectinload(Event.venue), selectinload(Event.organizer), selectinload(Event.evidence).selectinload(EventEvidence.raw_signal))
        .join(Event.venue)
    )
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(Event.title.ilike(like), Event.description.ilike(like)))
    if category:
        query = query.where(Event.category.in_([c.strip() for c in category.split(",") if c.strip()]))
    if not include_past:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        query = query.where(or_(Event.start_time.is_(None), Event.start_time >= cutoff, Event.end_time >= cutoff))
    query, _exact = radius_events_query(db, query, lat, lng, radius_km)
    rows = db.execute(query.limit(MAX_CANDIDATES)).scalars().unique().all()

    hits: list[tuple[Event, float]] = []
    for e in rows:
        if not e.venue.has_coordinates:
            continue
        d = haversine_km(lat, lng, e.venue.latitude, e.venue.longitude)
        if d <= radius_km:
            hits.append((e, round(d, 2)))

    far_future = datetime.max.replace(tzinfo=timezone.utc)
    if sort == "distance":
        hits.sort(key=lambda t: (t[1], t[0].start_time or far_future))
    elif sort == "confidence":
        hits.sort(key=lambda t: (-t[0].confidence_score, t[1]))
    else:   # time: soonest first, nearer first on ties
        hits.sort(key=lambda t: (t[0].start_time or far_future, t[1]))
    total = len(hits)
    return total, hits[(page - 1) * page_size:(page - 1) * page_size + page_size]


@router.get("/discover", response_model=DiscoverOut)
def discover(
    request: Request,
    db: Session = Depends(get_db),
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float | None = Query(default=None, gt=0),
    q: str | None = Query(default=None, max_length=255),
    category: str | None = Query(default=None, description="comma-separated"),
    include_past: bool = False,
    sort: str = Query(default="time", pattern="^(time|distance|confidence)$"),
    refresh: bool = Query(default=False, description="Force a live scan even if the area is fresh"),
    scan: bool = Query(default=True, description="Set false to only read what's already in the database"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    from app.services import ratelimit
    from app.services.discovery import ScanReport, coverage_status
    from app.services.ip_geo import client_ip

    radius = min(radius_km or settings.discover_default_radius_km, settings.discover_max_radius_km)
    is_admin = getattr(request.state, "is_admin", True)
    place = geocoding.reverse(db, lat, lng)

    def _read_only(reason_override: str | None = None) -> ScanReport:
        reason, last, rows = coverage_status(db, lat, lng, radius)
        return ScanReport(scanned=False, reason=reason_override or reason, city=place.city if place else None,
                          country=place.country if place else None,
                          country_code=place.country_code if place else None,
                          last_scanned_at=last, cells=len(rows))

    if not scan:
        report = _read_only()
    else:
        reason, _last, _rows = coverage_status(db, lat, lng, radius)
        needs_scan = refresh or reason != "fresh"
        # Anonymous visitors trigger live scans of third-party sources — cap them per IP.
        if needs_scan and not is_admin and not ratelimit.allow(
                f"scan:{client_ip(request) or 'unknown'}", settings.public_scans_per_hour):
            report = _read_only("rate_limited")
        else:
            report = scan_area(db, lat, lng, radius, query=q, force=refresh)
    db.commit()

    total, hits = _nearby_events(db, lat, lng, radius, q=q, category=category, include_past=include_past,
                                 sort=sort, page=page, page_size=page_size)
    items = []
    for ev, dist in hits:
        out = DiscoverEventOut.model_validate(ev)
        out.distance_km = dist
        items.append(out)
    center = PlaceOut(**(place.to_dict() if place else {"latitude": lat, "longitude": lng, "source": "none"}))
    return DiscoverOut(center=center, radius_km=radius, total=total, page=page, page_size=page_size,
                       items=items, coverage=CoverageOut(**report.to_dict()), sort=sort)


@router.post("/discover/scan", response_model=CoverageOut)
def force_scan(payload: ScanRequest, db: Session = Depends(get_db)):
    radius = min(payload.radius_km or settings.discover_default_radius_km, settings.discover_max_radius_km)
    report = scan_area(db, payload.lat, payload.lng, radius, query=payload.q, force=True)
    return CoverageOut(**report.to_dict())


@router.get("/discover/coverage", response_model=list[CoverageCellOut])
def list_coverage(
    db: Session = Depends(get_db),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=500, ge=1, le=5000),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(CoverageCell)
        .where(or_(CoverageCell.last_requested_at >= since, CoverageCell.last_scanned_at >= since))
        .order_by(CoverageCell.last_requested_at.desc().nullslast())
        .limit(limit)
    ).scalars().all()
    return rows


@router.get("/geocode", response_model=list[PlaceOut])
def geocode_search(db: Session = Depends(get_db), q: str = Query(min_length=2, max_length=200)):
    """Free-text place search for the city picker. Returns 0 or 1 result (provider's best match)."""
    place = geocoding.forward(db, q)
    db.commit()
    return [PlaceOut(**place.to_dict())] if place else []


@router.get("/geocode/reverse", response_model=PlaceOut)
def geocode_reverse(db: Session = Depends(get_db), lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180)):
    place = geocoding.reverse(db, lat, lng)
    db.commit()
    if place is None:
        raise HTTPException(status_code=404, detail="No place found for these coordinates")
    return PlaceOut(**place.to_dict())
