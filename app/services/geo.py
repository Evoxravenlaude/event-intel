"""
Geo utilities.

radius_events_query() is the single entry point for radius filtering.
On a PostgreSQL + PostGIS database it rewrites the SQLAlchemy query to use
ST_DWithin, which hits the spatial index and runs entirely in the database.
On SQLite (development / testing) it falls back to a Python-side Haversine
pass after the main query.

The PostGIS path requires the `postgis` extension to be enabled on the database.
Run the supabase/bootstrap.sql script to enable it (CREATE EXTENSION postgis).
"""
from __future__ import annotations
import math
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy.sql import Select


# ---------------------------------------------------------------------------
# Haversine (always available; used on SQLite and as safety net)
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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


def _postgis_available(db: Session) -> bool:
    """Return True if the `postgis` extension is installed. Result is cached."""
    global _postgis_available_cache
    if _postgis_available_cache is not None:
        return _postgis_available_cache
    if not _is_postgres(db):
        _postgis_available_cache = False
        return False
    try:
        result = db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
        ).scalar_one_or_none()
        _postgis_available_cache = result is not None
    except Exception:
        _postgis_available_cache = False
    return _postgis_available_cache


def pgvector_available(db: Session) -> bool:
    """Return True if the `vector` (pgvector) extension is installed. Result is cached."""
    global _pgvector_available_cache
    if _pgvector_available_cache is not None:
        return _pgvector_available_cache
    if not _is_postgres(db):
        _pgvector_available_cache = False
        return False
    try:
        result = db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
        _pgvector_available_cache = result is not None
    except Exception:
        _pgvector_available_cache = False
    return _pgvector_available_cache


# ---------------------------------------------------------------------------
# PostGIS radius filter
# ---------------------------------------------------------------------------

def radius_events_query(
    db: Session,
    query: "Select",
    lat: float,
    lng: float,
    radius_km: float,
) -> "tuple[Select, bool]":
    """
    Return (modified_query, postgis_used).

    When PostGIS is available the query gains a WHERE ST_DWithin clause that
    runs entirely inside the database against the spatial index.
    When PostGIS is not available, the original query is returned unchanged
    and the caller must do a Python-side Haversine pass.
    """
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
