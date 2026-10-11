"""
Geocoding: text ⇄ coordinates, globally, with caching and an offline fallback.

Why this exists
---------------
"Scan for events around me" needs two things the pipeline previously lacked:

  reverse(lat, lng)  → city / country       so per-city sources know what to ask for
                                             and venues get a real city label
  forward(text)      → lat / lng             so signals that only say "Landmark
                                             Centre, Lagos" become reachable by
                                             radius search at all

Provider
--------
Nominatim (OpenStreetMap) by default — free, global, no key, but max 1 req/s
and it requires an identifying User-Agent / email. GEOCODER_BASE_URL can point
at a self-hosted Nominatim or any compatible endpoint. Every answer is cached in
`geocode_cache` so a city or venue string is resolved at most once.

Offline fallback
----------------
If geocoding is disabled or the provider is unreachable, reverse lookups fall
back to a built-in gazetteer of ~90 major cities (nearest within 60 km) so the
product still says "Lagos" rather than nothing; forward lookups return None.
"""
from __future__ import annotations
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.event import GeocodeCache
from app.services.geo import haversine_km

logger = logging.getLogger(__name__)


@dataclass
class Place:
    latitude: float
    longitude: float
    city: str | None = None
    country: str | None = None
    country_code: str | None = None     # ISO 3166-1 alpha-2, upper-case
    display_name: str | None = None
    source: str = "geocoder"            # geocoder | gazetteer | cache

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Place":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Offline gazetteer — major cities, used when the geocoder is unavailable.
# (name, country, cc, lat, lng)
# ---------------------------------------------------------------------------

GAZETTEER: list[tuple[str, str, str, float, float]] = [
    ("Lagos", "Nigeria", "NG", 6.5244, 3.3792), ("Abuja", "Nigeria", "NG", 9.0765, 7.3986),
    ("Port Harcourt", "Nigeria", "NG", 4.8156, 7.0498), ("Accra", "Ghana", "GH", 5.6037, -0.1870),
    ("Nairobi", "Kenya", "KE", -1.2921, 36.8219), ("Kigali", "Rwanda", "RW", -1.9441, 30.0619),
    ("Kampala", "Uganda", "UG", 0.3476, 32.5825), ("Dar es Salaam", "Tanzania", "TZ", -6.7924, 39.2083),
    ("Addis Ababa", "Ethiopia", "ET", 9.0320, 38.7469), ("Cairo", "Egypt", "EG", 30.0444, 31.2357),
    ("Casablanca", "Morocco", "MA", 33.5731, -7.5898), ("Tunis", "Tunisia", "TN", 36.8065, 10.1815),
    ("Dakar", "Senegal", "SN", 14.7167, -17.4677), ("Abidjan", "Côte d'Ivoire", "CI", 5.3600, -4.0083),
    ("Libreville", "Gabon", "GA", 0.4162, 9.4673), ("Douala", "Cameroon", "CM", 4.0511, 9.7679),
    ("Kinshasa", "DR Congo", "CD", -4.4419, 15.2663), ("Luanda", "Angola", "AO", -8.8390, 13.2894),
    ("Johannesburg", "South Africa", "ZA", -26.2041, 28.0473), ("Cape Town", "South Africa", "ZA", -33.9249, 18.4241),
    ("London", "United Kingdom", "GB", 51.5074, -0.1278), ("Manchester", "United Kingdom", "GB", 53.4808, -2.2426),
    ("Paris", "France", "FR", 48.8566, 2.3522), ("Berlin", "Germany", "DE", 52.5200, 13.4050),
    ("Munich", "Germany", "DE", 48.1351, 11.5820), ("Amsterdam", "Netherlands", "NL", 52.3676, 4.9041),
    ("Brussels", "Belgium", "BE", 50.8503, 4.3517), ("Madrid", "Spain", "ES", 40.4168, -3.7038),
    ("Barcelona", "Spain", "ES", 41.3851, 2.1734), ("Lisbon", "Portugal", "PT", 38.7169, -9.1399),
    ("Rome", "Italy", "IT", 41.9028, 12.4964), ("Milan", "Italy", "IT", 45.4642, 9.1900),
    ("Zurich", "Switzerland", "CH", 47.3769, 8.5417), ("Vienna", "Austria", "AT", 48.2082, 16.3738),
    ("Prague", "Czechia", "CZ", 50.0755, 14.4378), ("Warsaw", "Poland", "PL", 52.2297, 21.0122),
    ("Stockholm", "Sweden", "SE", 59.3293, 18.0686), ("Copenhagen", "Denmark", "DK", 55.6761, 12.5683),
    ("Oslo", "Norway", "NO", 59.9139, 10.7522), ("Helsinki", "Finland", "FI", 60.1699, 24.9384),
    ("Dublin", "Ireland", "IE", 53.3498, -6.2603), ("Athens", "Greece", "GR", 37.9838, 23.7275),
    ("Istanbul", "Türkiye", "TR", 41.0082, 28.9784), ("Tel Aviv", "Israel", "IL", 32.0853, 34.7818),
    ("Dubai", "United Arab Emirates", "AE", 25.2048, 55.2708), ("Abu Dhabi", "United Arab Emirates", "AE", 24.4539, 54.3773),
    ("Doha", "Qatar", "QA", 25.2854, 51.5310), ("Riyadh", "Saudi Arabia", "SA", 24.7136, 46.6753),
    ("Mumbai", "India", "IN", 19.0760, 72.8777), ("Delhi", "India", "IN", 28.6139, 77.2090),
    ("Bangalore", "India", "IN", 12.9716, 77.5946), ("Hyderabad", "India", "IN", 17.3850, 78.4867),
    ("Karachi", "Pakistan", "PK", 24.8607, 67.0011), ("Lahore", "Pakistan", "PK", 31.5204, 74.3587),
    ("Dhaka", "Bangladesh", "BD", 23.8103, 90.4125), ("Colombo", "Sri Lanka", "LK", 6.9271, 79.8612),
    ("Bangkok", "Thailand", "TH", 13.7563, 100.5018), ("Ho Chi Minh City", "Vietnam", "VN", 10.8231, 106.6297),
    ("Hanoi", "Vietnam", "VN", 21.0278, 105.8342), ("Singapore", "Singapore", "SG", 1.3521, 103.8198),
    ("Kuala Lumpur", "Malaysia", "MY", 3.1390, 101.6869), ("Jakarta", "Indonesia", "ID", -6.2088, 106.8456),
    ("Manila", "Philippines", "PH", 14.5995, 120.9842), ("Hong Kong", "Hong Kong", "HK", 22.3193, 114.1694),
    ("Shanghai", "China", "CN", 31.2304, 121.4737), ("Beijing", "China", "CN", 39.9042, 116.4074),
    ("Shenzhen", "China", "CN", 22.5431, 114.0579), ("Taipei", "Taiwan", "TW", 25.0330, 121.5654),
    ("Seoul", "South Korea", "KR", 37.5665, 126.9780), ("Tokyo", "Japan", "JP", 35.6762, 139.6503),
    ("Osaka", "Japan", "JP", 34.6937, 135.5023), ("Sydney", "Australia", "AU", -33.8688, 151.2093),
    ("Melbourne", "Australia", "AU", -37.8136, 144.9631), ("Auckland", "New Zealand", "NZ", -36.8485, 174.7633),
    ("New York", "United States", "US", 40.7128, -74.0060), ("Los Angeles", "United States", "US", 34.0522, -118.2437),
    ("San Francisco", "United States", "US", 37.7749, -122.4194), ("Chicago", "United States", "US", 41.8781, -87.6298),
    ("Miami", "United States", "US", 25.7617, -80.1918), ("Austin", "United States", "US", 30.2672, -97.7431),
    ("Seattle", "United States", "US", 47.6062, -122.3321), ("Boston", "United States", "US", 42.3601, -71.0589),
    ("Washington", "United States", "US", 38.9072, -77.0369), ("Atlanta", "United States", "US", 33.7490, -84.3880),
    ("Toronto", "Canada", "CA", 43.6532, -79.3832), ("Vancouver", "Canada", "CA", 49.2827, -123.1207),
    ("Montreal", "Canada", "CA", 45.5017, -73.5673), ("Mexico City", "Mexico", "MX", 19.4326, -99.1332),
    ("Bogotá", "Colombia", "CO", 4.7110, -74.0721), ("Medellín", "Colombia", "CO", 6.2442, -75.5812),
    ("Lima", "Peru", "PE", -12.0464, -77.0428), ("Santiago", "Chile", "CL", -33.4489, -70.6693),
    ("Buenos Aires", "Argentina", "AR", -34.6037, -58.3816), ("São Paulo", "Brazil", "BR", -23.5505, -46.6333),
    ("Rio de Janeiro", "Brazil", "BR", -22.9068, -43.1729),
]

GAZETTEER_MAX_KM = 60.0


def nearest_gazetteer_city(lat: float, lng: float, max_km: float = GAZETTEER_MAX_KM) -> Place | None:
    best, best_d = None, max_km
    for name, country, cc, clat, clng in GAZETTEER:
        d = haversine_km(lat, lng, clat, clng)
        if d < best_d:
            best_d, best = d, (name, country, cc, clat, clng)
    if best is None:
        return None
    name, country, cc, clat, clng = best
    return Place(latitude=lat, longitude=lng, city=name, country=country, country_code=cc,
                 display_name=f"{name}, {country}", source="gazetteer")


def gazetteer_lookup(text: str) -> Place | None:
    """Exact (case-insensitive) city-name hit in the gazetteer."""
    t = text.strip().lower()
    for name, country, cc, clat, clng in GAZETTEER:
        if t == name.lower() or t == f"{name}, {country}".lower():
            return Place(latitude=clat, longitude=clng, city=name, country=country, country_code=cc,
                         display_name=f"{name}, {country}", source="gazetteer")
    return None


# ---------------------------------------------------------------------------
# Provider (Nominatim-compatible)
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_last_call = 0.0
_provider_down_until = 0.0   # circuit breaker: skip the provider for a while after failures


def _throttle() -> None:
    global _last_call
    with _rate_lock:
        wait = settings.geocoder_min_interval_seconds - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.geocoder_base_url.rstrip("/"),
        timeout=settings.geocoder_timeout_seconds,
        headers={"User-Agent": settings.geocoder_user_agent, "Accept-Language": "en"},
    )


def _provider_available() -> bool:
    return settings.geocoding_enabled and time.monotonic() >= _provider_down_until


def _trip_breaker(exc: Exception) -> None:
    global _provider_down_until
    _provider_down_until = time.monotonic() + settings.geocoder_backoff_seconds
    logger.warning("Geocoder unavailable (%s) — using offline fallback for %ds", exc, settings.geocoder_backoff_seconds)


def _parse_nominatim(item: dict) -> Place | None:
    try:
        lat, lng = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    addr = item.get("address") or {}
    city = (addr.get("city") or addr.get("town") or addr.get("municipality") or addr.get("village")
            or addr.get("county") or addr.get("state_district") or addr.get("state"))
    cc = (addr.get("country_code") or "").upper() or None
    return Place(latitude=lat, longitude=lng, city=city, country=addr.get("country"), country_code=cc,
                 display_name=item.get("display_name"), source="geocoder")


def _provider_reverse(lat: float, lng: float) -> Place | None:
    _throttle()
    with _client() as c:
        r = c.get("/reverse", params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 10, "addressdetails": 1})
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict) or data.get("error"):
        return None
    place = _parse_nominatim(data)
    if place:
        place.latitude, place.longitude = lat, lng   # keep the query point, not the matched feature
    return place


def _provider_forward(text: str) -> Place | None:
    _throttle()
    with _client() as c:
        r = c.get("/search", params={"q": text, "format": "jsonv2", "limit": 1, "addressdetails": 1})
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, list) or not data:
        return None
    return _parse_nominatim(data[0])


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _key(kind: str, *parts) -> str:
    raw = kind + "|" + "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_get(db: Session, key: str) -> Place | None:
    row = db.execute(select(GeocodeCache).where(GeocodeCache.key == key)).scalar_one_or_none()
    if row is None:
        return None
    if row.result is None:
        return None   # cached miss
    try:
        place = Place.from_dict(json.loads(row.result))
        place.source = "cache"
        return place
    except (ValueError, TypeError):
        return None


def _cache_has(db: Session, key: str) -> bool:
    return db.execute(select(GeocodeCache.id).where(GeocodeCache.key == key)).scalar_one_or_none() is not None


def _cache_put(db: Session, key: str, query: str, place: Place | None) -> None:
    """
    Best-effort cache write. Runs in a SAVEPOINT so that losing a race with
    another worker (unique violation on `key`) rolls back only this insert —
    previously it poisoned the caller's whole transaction and crashed the
    cluster pass with PendingRollbackError.
    """
    payload = json.dumps(place.to_dict()) if place else None
    try:
        with db.begin_nested():
            row = db.execute(select(GeocodeCache).where(GeocodeCache.key == key)).scalar_one_or_none()
            if row:
                row.result = payload
                row.created_at = datetime.now(timezone.utc)
            else:
                db.add(GeocodeCache(key=key, query=query[:500], result=payload))
    except IntegrityError:
        logger.debug("geocode cache key %s written concurrently — keeping the other copy", key)
    except Exception as exc:  # the cache must never break the caller
        logger.warning("geocode cache write failed for %s: %s", key, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reverse(db: Session, lat: float, lng: float) -> Place | None:
    """lat/lng → Place with city/country. Never raises; falls back to the gazetteer."""
    # Round to ~1km so nearby points share a cache entry
    key = _key("rev", round(lat, 2), round(lng, 2))
    cached = _cache_get(db, key)
    if cached:
        cached.latitude, cached.longitude = lat, lng
        return cached
    place: Place | None = None
    if _provider_available():
        fetched = False
        try:
            place = _provider_reverse(lat, lng)
            fetched = True
        except Exception as exc:   # network / HTTP / JSON — the provider, not us
            _trip_breaker(exc)
        if fetched:
            _cache_put(db, key, f"{lat},{lng}", place)
    if place is None:
        place = nearest_gazetteer_city(lat, lng)
    return place


def forward(db: Session, text: str, city_hint: str | None = None) -> Place | None:
    """
    Free text ("Landmark Centre, Lagos", "Berlin") → Place. Never raises.
    `city_hint` is appended when the text doesn't already mention it, which
    turns venue names into resolvable queries.
    """
    text = (text or "").strip()
    if not text:
        return None
    query = text
    if city_hint and city_hint.lower() not in text.lower():
        query = f"{text}, {city_hint}"
    key = _key("fwd", query.lower())
    if _cache_has(db, key):
        return _cache_get(db, key)
    place: Place | None = None
    if _provider_available():
        fetched = False
        try:
            place = _provider_forward(query)
            if place is None and query != text:
                place = _provider_forward(text)
            fetched = True
        except Exception as exc:   # network / HTTP / JSON — the provider, not us
            _trip_breaker(exc)
        if fetched:
            _cache_put(db, key, query, place)
    if place is None:
        place = gazetteer_lookup(text) or (gazetteer_lookup(city_hint) if city_hint else None)
    return place


def reset_breaker() -> None:
    """For tests."""
    global _provider_down_until, _last_call
    _provider_down_until = 0.0
    _last_call = 0.0
