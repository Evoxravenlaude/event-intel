"""
Location-first discovery: "what is happening around this point?"

Flow for /discover?lat&lng&radius_km
------------------------------------
1. reverse-geocode the point → city / country (cache, then gazetteer fallback)
2. look for a recent scan circle (centre + radius, `coverage_cells`) that
   contains the request circle
3. if there is none — never scanned, only partially covered, or older than
   DISCOVER_STALE_MINUTES (or refresh=true) — run a live scan:
     every geo-capable source in DISCOVER_SOURCES is asked for lat/lng/radius,
     signals are stored, a cluster pass runs, the scan circle is recorded
4. return the events within the radius (same query as /events) plus a
   `coverage` block so the client can show "scanned 4 min ago · 2 sources"

Areas people actually look at get re-warmed by the scheduler pseudo-source
"coverage" (see rescan_recent_cells), so a second visitor to the same city
gets an instant answer from the DB.

The scan is synchronous and bounded: at most DISCOVER_MAX_SCAN_SECONDS of
source time, then whatever we have is returned. Sources with no API key
are skipped (or mocked when ENABLE_MOCK_ADAPTERS is on).
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.event import CoverageCell
from app.services import geocoding
from app.services.adapters import ADAPTERS, GeoQuery
from app.services.clustering import cluster_signals
from app.services.event_service import create_signal, create_source_run, finish_source_run
from app.services.geo import bounding_box, haversine_km
from app.services.geohash import encode

logger = logging.getLogger(__name__)

MAX_SCAN_SECONDS = 25


@dataclass
class ScanReport:
    scanned: bool
    reason: str                       # "fresh" | "never_scanned" | "stale" | "forced" | "no_sources"
    city: str | None
    country: str | None
    country_code: str | None
    sources_run: list[str] = field(default_factory=list)
    sources_skipped: list[str] = field(default_factory=list)
    signals_created: int = 0
    events_created: int = 0
    signals_linked: int = 0
    queued_for_review: int = 0
    errors: list[str] = field(default_factory=list)
    last_scanned_at: datetime | None = None
    cells: int = 0

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned, "reason": self.reason,
            "city": self.city, "country": self.country, "country_code": self.country_code,
            "sources_run": self.sources_run, "sources_skipped": self.sources_skipped,
            "signals_created": self.signals_created, "events_created": self.events_created,
            "signals_linked": self.signals_linked, "queued_for_review": self.queued_for_review,
            "errors": self.errors, "last_scanned_at": self.last_scanned_at, "cells": self.cells,
        }


def _active_geo_sources(only: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(usable, skipped) source names for a live scan."""
    wanted = only or settings.split_csv(settings.discover_sources) or [n for n, a in ADAPTERS.items() if a.supports_geo]
    usable, skipped = [], []
    for name in wanted:
        adapter = ADAPTERS.get(name)
        if adapter is None or not adapter.supports_geo:
            skipped.append(name)
            continue
        if adapter.is_configured() or settings.enable_mock_adapters:
            usable.append(name)
        else:
            skipped.append(name)
    return usable, skipped


CENTRE_PRECISION = 6          # ~1.2 km: scans from "the same spot" share a row
CONTAIN_TOLERANCE_KM = 2.0    # slack when testing whether a scan circle contains the request


def _nearby_scans(db: Session, lat: float, lng: float, radius_km: float) -> list[CoverageCell]:
    """Scan areas whose circle could intersect the request circle (bounding-box prefilter)."""
    reach = radius_km + settings.discover_max_radius_km
    min_lat, max_lat, min_lng, max_lng = bounding_box(lat, lng, reach)
    return list(db.execute(
        select(CoverageCell).where(
            CoverageCell.latitude.between(min_lat, max_lat),
            CoverageCell.longitude.between(min_lng, max_lng),
        )
    ).scalars().all())


def coverage_status(db: Session, lat: float, lng: float, radius_km: float) -> tuple[str, datetime | None, list[CoverageCell]]:
    """
    (reason, last_scan, candidate_rows) — reason is:
      fresh          a recent scan circle contains the request circle
      stale          only old scans cover it (or recent ones cover it partially)
      never_scanned  no scan circle even intersects it
    """
    rows = _nearby_scans(db, lat, lng, radius_km)
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=settings.discover_stale_minutes)
    best_recent: datetime | None = None
    intersects = False
    latest_any: datetime | None = None
    for row in rows:
        if row.last_scanned_at is None:
            continue
        d = haversine_km(lat, lng, row.latitude, row.longitude)
        if d > row.radius_km + radius_km:
            continue
        intersects = True
        latest_any = row.last_scanned_at if latest_any is None else max(latest_any, row.last_scanned_at)
        contains = d + radius_km <= row.radius_km + CONTAIN_TOLERANCE_KM
        if contains and row.last_scanned_at >= stale_before:
            best_recent = row.last_scanned_at if best_recent is None else max(best_recent, row.last_scanned_at)
    if best_recent is not None:
        return "fresh", best_recent, rows
    if intersects:
        return "stale", latest_any, rows
    return "never_scanned", None, rows


def _record_scan(db: Session, lat: float, lng: float, radius_km: float, place: geocoding.Place | None,
                 *, scanned: bool, signal_count: int = 0) -> CoverageCell:
    now = datetime.now(timezone.utc)
    key = encode(lat, lng, CENTRE_PRECISION)
    row = db.execute(select(CoverageCell).where(CoverageCell.cell == key)).scalar_one_or_none()
    if row is None:
        row = CoverageCell(cell=key, latitude=lat, longitude=lng, radius_km=radius_km)
        db.add(row)
    row.last_requested_at = now
    if place:
        row.city = row.city or place.city
        row.country_code = row.country_code or place.country_code
    if scanned:
        # A fresh scan with a larger radius supersedes; a smaller one must not shrink the record
        # unless the old one has gone stale (then the new radius is what's actually fresh).
        stale_before = now - timedelta(minutes=settings.discover_stale_minutes)
        if row.last_scanned_at is None or row.last_scanned_at < stale_before or radius_km >= row.radius_km:
            row.radius_km = radius_km
        row.last_scanned_at = now
        row.scan_count = (row.scan_count or 0) + 1
        row.last_signal_count = signal_count
    else:
        row.radius_km = max(row.radius_km or 0, radius_km) if row.last_scanned_at is None else row.radius_km
    db.flush()
    return row


def scan_area(db: Session, lat: float, lng: float, radius_km: float, *, query: str | None = None,
              force: bool = False, sources: list[str] | None = None) -> ScanReport:
    """Run the live scan if needed and return what happened."""
    radius_km = min(max(radius_km, 1.0), settings.discover_max_radius_km)
    place = geocoding.reverse(db, lat, lng)
    reason, last_scanned, rows = coverage_status(db, lat, lng, radius_km)
    report = ScanReport(
        scanned=False, reason="forced" if force else reason,
        city=place.city if place else None, country=place.country if place else None,
        country_code=place.country_code if place else None,
        last_scanned_at=last_scanned, cells=len(rows),
    )

    if not force and reason == "fresh":
        _record_scan(db, lat, lng, radius_km, place, scanned=False)
        db.commit()
        return report

    usable, skipped = _active_geo_sources(sources)
    report.sources_skipped = skipped
    if not usable:
        report.reason = "no_sources"
        _record_scan(db, lat, lng, radius_km, place, scanned=False)
        db.commit()
        return report

    geo = GeoQuery(lat=lat, lng=lng, radius_km=radius_km)
    city = place.city if place else None
    started = time.monotonic()
    created_total = 0
    for name in usable:
        if time.monotonic() - started > MAX_SCAN_SECONDS:
            report.errors.append(f"{name}: skipped — scan time budget exhausted")
            report.sources_skipped.append(name)
            continue
        run = create_source_run(db, source=name, city=city, query=query)
        try:
            result = ADAPTERS[name].fetch(city=city, query=query, urls=None, geo=geo)
            created = [create_signal(db, item) for item in result.items]
            status = "completed" if not result.errors else ("partial" if created else "failed")
            finish_source_run(db, run, status=status, fetched_count=result.fetched_count,
                              created_signal_count=len(created), error="; ".join(result.errors) or None)
            created_total += len(created)
            report.sources_run.append(name)
            report.errors.extend(result.errors)
        except Exception as exc:  # one broken source must not sink the scan
            db.rollback()
            finish_source_run(db, run, status="failed", fetched_count=0, created_signal_count=0, error=str(exc))
            report.errors.append(f"{name}: {exc}")
            logger.exception("Discovery scan source %s failed", name)

    report.signals_created = created_total
    new_events, linked, queued = cluster_signals(db)
    report.events_created, report.signals_linked, report.queued_for_review = len(new_events), len(linked), len(queued)
    report.scanned = True
    report.last_scanned_at = datetime.now(timezone.utc)
    _record_scan(db, lat, lng, radius_km, place, scanned=True, signal_count=created_total)
    db.commit()
    logger.info("discover scan %s,%s r=%skm (%s): %d signals, %d new events via %s",
                lat, lng, radius_km, city, created_total, len(new_events), report.sources_run)
    return report


def rescan_recent_cells(db: Session) -> dict:
    """
    Scheduler job: re-scan every distinct area requested in the last
    COVERAGE_RESCAN_DAYS. Cells are grouped by city so one scan per city
    (centred on the mean of its cells, radius covering them) is enough.
    """
    since = datetime.now(timezone.utc) - timedelta(days=settings.coverage_rescan_days)
    rows = db.execute(select(CoverageCell).where(CoverageCell.last_requested_at >= since)).scalars().all()
    if not rows:
        return {"status": "completed", "areas": 0, "signals": 0}
    groups: dict[str, list[CoverageCell]] = {}
    for r in rows:
        groups.setdefault(r.city or r.cell[:3], []).append(r)
    areas = 0
    signals = 0
    for key, cells in groups.items():
        lat = sum(c.latitude for c in cells) / len(cells)
        lng = sum(c.longitude for c in cells) / len(cells)
        radius = min(
            settings.discover_max_radius_km,
            max(haversine_km(lat, lng, c.latitude, c.longitude) + (c.radius_km or 5.0) for c in cells),
        )
        report = scan_area(db, lat, lng, radius, force=True)
        areas += 1
        signals += report.signals_created
    return {"status": "completed", "areas": areas, "signals": signals}
