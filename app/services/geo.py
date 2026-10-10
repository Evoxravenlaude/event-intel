"""
Geo utilities.

radius_events_query() is the single entry point for radius filtering.
On a PostgreSQL + PostGIS database it rewrites the SQLAlchemy query to use
ST_DWithin, which hits the spatial index and runs entirely in the database.
On any backend it first applies a cheap latitude/longitude bounding box
(index-backed on venues.latitude / venues.longitude); callers then run an
exact Haversine pass in Python when PostGIS was not used.

The PostGIS path requires the `postgis` extension to be enabled on the database.
Run the supabase/bootstrap.sql script to enable it (CREATE EXTENSION postgis).
"""
from __future__ import annotations
import math
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.event import Venue

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

EARTH_RADIUS_KM = 6371.0
KM_PER_DEG_LAT = 111.32


# ---------------------------------------------------------------------------
# Haversine (always available; used on SQLite and as safety net)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bounding_box(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lng, max_lng) enclosing the circle."""
    d_lat = radius_km / KM_PER_DEG_LAT
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    d_lng = radius_km / (KM_PER_DEG_LAT * cos_lat)
    return (
        max(lat - d_lat, -90.0),
        min(lat + d_lat, 90.0),
        max(lng - d_lng, -180.0),
        min(lng + d_lng, 180.0),
    )


# ---------------------------------------------------------------------------
# Dialect detection — SA 2.0 compatible
# ---------------------------------------------------------------------------

def _dialect_name(db: Session) -> str:
    """
    Return the dialect name ('postgresql', 'sqlite', etc.) for the given session.

    Session.bind was removed in SQLAlchemy 2.0.  The correct approach is to
    call Session.connection() which returns the active Connection, whose
    .dialect attribute is always available.
    """
    try:
        return db.connection().dialect.name
    except Exception:
        # Fall back gracefully — assume non-Postgres so we never accidentally
        # attempt PostGIS queries on an incompatible backend.
        return "unknown"


# Module-level cache so we hit pg_extension at most once per process lifetime.
# None = not yet checked; True/False = cached result.
_postgis_available_cache: bool | None = None
_pgvector_available_cache: bool | None = None


def _is_postgres(db: Session) -> bool:
    return _dialect_name(db) == "postgresql"


def _extension_installed(db: Session, name: str) -> bool:
    try:
        result = db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = :name").bindparams(name=name)
        ).scalar_one_or_none()
        return result is not None
    except Exception:
        return False


def _postgis_available(db: Session) -> bool:
    """Return True if the `postgis` extension is installed. Result is cached."""
    global _postgis_available_cache
    if _postgis_available_cache is not None:
        return _postgis_available_cache
    if not _is_postgres(db):
        _postgis_available_cache = False
        return False
    # The location column only exists if migration 0002's PostGIS block succeeded
    _postgis_available_cache = _extension_installed(db, "postgis") and _column_exists(db, "venues", "location")
    return _postgis_available_cache


def _column_exists(db: Session, table: str, column: str) -> bool:
    try:
        result = db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ).bindparams(t=table, c=column)
        ).scalar_one_or_none()
        return result is not None
    except Exception:
        return False


def pgvector_available(db: Session) -> bool:
    """Return True if the `vector` (pgvector) extension is installed. Result is cached."""
    global _pgvector_available_cache
    if _pgvector_available_cache is not None:
        return _pgvector_available_cache
    if not _is_postgres(db):
        _pgvector_available_cache = False
        return False
    _pgvector_available_cache = _extension_installed(db, "vector")
    return _pgvector_available_cache


def reset_capability_cache() -> None:
    """For tests / after running bootstrap.sql in a live process."""
    global _postgis_available_cache, _pgvector_available_cache
    _postgis_available_cache = None
    _pgvector_available_cache = None


# ---------------------------------------------------------------------------
# Radius filter
# ---------------------------------------------------------------------------

def radius_events_query(
    db: Session,
    query: "Select",
    lat: float,
    lng: float,
    radius_km: float,
) -> "tuple[Select, bool]":
    """
    Return (modified_query, exact).

    The query always gains an index-friendly bounding-box filter on the
    venues table (the caller must have joined Event.venue).

    When PostGIS is available an exact ST_DWithin clause is added as well and
    `exact` is True — no further filtering is needed.
    When it isn't, `exact` is False and the caller must run a Haversine pass
    over the (bounded) result set before paginating.
    """
    min_lat, max_lat, min_lng, max_lng = bounding_box(lat, lng, radius_km)
    query = query.where(
        Venue.latitude.is_not(None),
        Venue.longitude.is_not(None),
        Venue.latitude.between(min_lat, max_lat),
        Venue.longitude.between(min_lng, max_lng),
    )

    if not _postgis_available(db):
        return query, False

    radius_m = radius_km * 1000
    spatial_filter = text(
        "ST_DWithin("
        "  venues.location::geography,"
        "  ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,"
        "  :radius_m"
        ")"
    ).bindparams(lat=lat, lng=lng, radius_m=radius_m)
    return query.where(spatial_filter), True
