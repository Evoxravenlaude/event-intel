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
import re
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


# How precise a coordinate is. Ordered best → worst.
ACCURACY_LEVELS = ("exact", "street", "area", "city", "region")


def worst_accuracy(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    return a if ACCURACY_LEVELS.index(a) >= ACCURACY_LEVELS.index(b) else b


@dataclass
class Place:
    latitude: float
    longitude: float
    city: str | None = None             # the city people would name (metro-normalised)
    country: str | None = None
    country_code: str | None = None     # ISO 3166-1 alpha-2, upper-case
    display_name: str | None = None
    source: str = "geocoder"            # google | nominatim | gazetteer | ip | cache
    neighbourhood: str | None = None    # Victoria Island, Shoreditch, Westlands …
    locality: str | None = None         # raw administrative locality, if different from city
    accuracy: str | None = None         # exact | street | area | city | region

    @property
    def label(self) -> str | None:
        """Short human label: "Victoria Island, Lagos" / "Lagos, Nigeria"."""
        first = self.neighbourhood or (self.locality if self.locality and self.locality != self.city else None)
        if first and self.city and first.lower() != self.city.lower():
            return f"{first}, {self.city}"
        if self.city:
            return f"{self.city}, {self.country}" if self.country else self.city
        return self.display_name

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label"] = self.label
        return d

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
                 display_name=f"{name}, {country}", source="gazetteer", accuracy="city")


def gazetteer_lookup(text: str) -> Place | None:
    """Exact (case-insensitive) city-name hit in the gazetteer."""
    t = text.strip().lower()
    for name, country, cc, clat, clng in GAZETTEER:
        if t == name.lower() or t == f"{name}, {country}".lower():
            return Place(latitude=clat, longitude=clng, city=name, country=country, country_code=cc,
                         display_name=f"{name}, {country}", source="gazetteer", accuracy="city")
    return None


def gazetteer_prefix(text: str, limit: int = 6) -> list[Place]:
    """Gazetteer cities whose name starts with `text` (offline autocomplete fallback)."""
    t = text.strip().lower()
    if not t:
        return []
    out = []
    for name, country, cc, clat, clng in GAZETTEER:
        if name.lower().startswith(t):
            out.append(Place(latitude=clat, longitude=clng, city=name, country=country, country_code=cc,
                             display_name=f"{name}, {country}", source="gazetteer", accuracy="city"))
    return out[:limit]


METRO_RADIUS_KM = 40.0


def _metro_city(lat: float, lng: float) -> str | None:
    """Nearest major city within METRO_RADIUS_KM — names the metro when the provider gives no city."""
    p = nearest_gazetteer_city(lat, lng, max_km=METRO_RADIUS_KM)
    return p.city if p else None


_ADMIN_SUFFIX = re.compile(r"\s+(State|Region|Province|County|Prefecture|Governorate|Municipality|District)$", re.I)


def _strip_admin(name: str | None) -> str | None:
    return _ADMIN_SUFFIX.sub("", name).strip() if name else None


# ---------------------------------------------------------------------------
# Providers
#
# Chain: Google Geocoding (if GOOGLE_MAPS_API_KEY) → Nominatim (if
# GEOCODING_ENABLED) → built-in gazetteer. A provider that *answers* (even
# "no result") ends the chain; only errors fall through to the next one.
# Each provider has its own circuit breaker.
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    pass


_rate_lock = threading.Lock()
_last_call = 0.0
_provider_down_until = 0.0   # Nominatim breaker
_google_down_until = 0.0     # Google breaker


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


def _google_client() -> httpx.Client:
    return httpx.Client(timeout=settings.geocoder_timeout_seconds)


def _provider_available() -> bool:
    """Nominatim usable right now."""
    return settings.geocoding_enabled and time.monotonic() >= _provider_down_until


def _google_available() -> bool:
    return bool(settings.google_maps_api_key) and time.monotonic() >= _google_down_until


def _trip_breaker(exc: Exception) -> None:
    global _provider_down_until
    _provider_down_until = time.monotonic() + settings.geocoder_backoff_seconds
    logger.warning("Geocoder unavailable (%s) — using offline fallback for %ds", exc, settings.geocoder_backoff_seconds)


def _trip_google(exc: Exception) -> None:
    global _google_down_until
    _google_down_until = time.monotonic() + settings.geocoder_backoff_seconds
    logger.warning("Google geocoding unavailable (%s) — falling back for %ds", exc, settings.geocoder_backoff_seconds)


# ---- Nominatim ----

def _nominatim_accuracy(item: dict) -> str:
    try:
        rank = int(item.get("place_rank"))
    except (TypeError, ValueError):
        return "area"
    if rank >= 28:
        return "exact"
    if rank >= 26:
        return "street"
    if rank >= 17:
        return "area"
    if rank >= 13:
        return "city"
    return "region"


def _parse_nominatim(item: dict) -> Place | None:
    try:
        lat, lng = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    addr = item.get("address") or {}
    raw_city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
    # Many metros (Lagos is the canonical case) have no `city` key at all — only
    # LGAs/counties ("Eti Osa"). Name the metro instead of the LGA.
    city = raw_city or _metro_city(lat, lng) or _strip_admin(addr.get("state")) or addr.get("county")
    neighbourhood = (addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter")
                     or addr.get("city_district") or addr.get("borough"))
    cc = (addr.get("country_code") or "").upper() or None
    return Place(
        latitude=lat, longitude=lng, city=city, country=addr.get("country"), country_code=cc,
        display_name=item.get("display_name"), source="nominatim",
        neighbourhood=neighbourhood if neighbourhood != city else None,
        locality=raw_city if raw_city and raw_city != city else None,
        accuracy=_nominatim_accuracy(item),
    )


def _provider_reverse(lat: float, lng: float) -> Place | None:
    _throttle()
    with _client() as c:
        # zoom 16 = street/neighbourhood level, so we get "Victoria Island" not just the region
        r = c.get("/reverse", params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 16, "addressdetails": 1})
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


# ---- Google Geocoding API ----

_GOOGLE_LOCATION_TYPE = {"ROOFTOP": "exact", "RANGE_INTERPOLATED": "street", "GEOMETRIC_CENTER": "area", "APPROXIMATE": "city"}


def parse_google_components(components: list[dict], long_key: str = "long_name", short_key: str = "short_name") -> dict:
    """Shared by the Geocoding API (long_name) and Places API New (longText)."""
    out: dict = {}
    for comp in components or []:
        types = comp.get("types") or []
        long_v, short_v = comp.get(long_key), comp.get(short_key)
        if "locality" in types:
            out.setdefault("locality", long_v)
        elif "postal_town" in types:
            out.setdefault("postal_town", long_v)
        if "sublocality_level_1" in types or "sublocality" in types:
            out.setdefault("sublocality", long_v)
        if "neighborhood" in types:
            out.setdefault("neighborhood", long_v)
        if "administrative_area_level_2" in types:
            out.setdefault("admin2", long_v)
        if "administrative_area_level_1" in types:
            out.setdefault("admin1", long_v)
        if "country" in types:
            out.setdefault("country", long_v)
            out.setdefault("country_code", (short_v or "").upper() or None)
    return out


def place_from_google_components(lat: float, lng: float, comps: dict, *, accuracy: str | None,
                                 display_name: str | None, source: str = "google") -> Place:
    raw_city = comps.get("locality") or comps.get("postal_town")
    city = raw_city or _metro_city(lat, lng) or _strip_admin(comps.get("admin1")) or comps.get("admin2")
    neighbourhood = comps.get("sublocality") or comps.get("neighborhood")
    return Place(
        latitude=lat, longitude=lng, city=city, country=comps.get("country"),
        country_code=comps.get("country_code"), display_name=display_name, source=source,
        neighbourhood=neighbourhood if neighbourhood and neighbourhood != city else None,
        locality=raw_city if raw_city and raw_city != city else None, accuracy=accuracy,
    )


def _google_geocode(params: dict) -> Place | None:
    with _google_client() as c:
        r = c.get(settings.google_geocode_url,
                  params={**params, "key": settings.google_maps_api_key, "language": "en"})
        r.raise_for_status()
        data = r.json()
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        raise ProviderError(f"google geocode {status}: {data.get('error_message', '')}".strip())
    results = data.get("results") or []
    if not results:
        return None
    first = results[0]
    loc = (first.get("geometry") or {}).get("location") or {}
    comps = parse_google_components(first.get("address_components") or [])
    # Reverse results are ordered most → least specific; borrow a locality from later ones if needed
    if not (comps.get("locality") or comps.get("postal_town")):
        for other in results[1:]:
            o = parse_google_components(other.get("address_components") or [])
            if o.get("locality"):
                comps["locality"] = o["locality"]
                break
    accuracy = _GOOGLE_LOCATION_TYPE.get((first.get("geometry") or {}).get("location_type"), "area")
    types = set(first.get("types") or [])
    if accuracy == "area" and types & {"route", "premise", "street_address", "establishment", "point_of_interest"}:
        accuracy = "street"
    if accuracy == "city" and not types & {"locality", "administrative_area_level_1", "administrative_area_level_2", "country"}:
        accuracy = "area"
    return place_from_google_components(
        float(loc["lat"]), float(loc["lng"]), comps, accuracy=accuracy, display_name=first.get("formatted_address"),
    )


def _google_reverse(lat: float, lng: float) -> Place | None:
    place = _google_geocode({"latlng": f"{lat},{lng}"})
    if place:
        place.latitude, place.longitude = lat, lng
    return place


def _google_forward(text: str) -> Place | None:
    return _google_geocode({"address": text})


def _chain(google_fn, nominatim_fn, *args) -> tuple[Place | None, bool]:
    """Run the provider chain. Returns (place, answered) — answered=False means every provider errored."""
    if _google_available():
        try:
            return google_fn(*args), True
        except Exception as exc:
            _trip_google(exc)
    if _provider_available():
        try:
            return nominatim_fn(*args), True
        except Exception as exc:
            _trip_breaker(exc)
    return None, False


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
    """lat/lng → Place with neighbourhood/city/country. Never raises; gazetteer fallback."""
    # ~110 m buckets: fine enough to keep the neighbourhood right, coarse enough to cache well
    key = _key("rev2", round(lat, 3), round(lng, 3))
    cached = _cache_get(db, key)
    if cached:
        cached.latitude, cached.longitude = lat, lng
        return cached
    place, answered = _chain(_google_reverse, _provider_reverse, lat, lng)
    if answered:
        _cache_put(db, key, f"{lat},{lng}", place)
    if place is None:
        place = nearest_gazetteer_city(lat, lng)
    return place


_POSTAL = re.compile(r"\b\d{5,6}\b")


def forward_candidates(text: str, city_hint: str | None = None) -> list[tuple[str, str | None]]:
    """
    Progressively simpler queries for an address, with the best accuracy each can claim:
      "Ilé MIMU, 1 Adeola Odeku St, Victoria Island, Lagos 106104, Lagos, Nigeria"
        → full text                                         (provider accuracy)
        → "1 Adeola Odeku St, Victoria Island, Lagos, Nigeria"  (venue name & postcode dropped)
        → "Victoria Island, Lagos, Nigeria"                   (area at best)
    """
    text = re.sub(r"\s+", " ", text).strip(" ,")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    full = text
    if city_hint and city_hint.lower() not in text.lower():
        full = f"{text}, {city_hint}"
    cands: list[tuple[str, str | None]] = [(full, None)]
    if len(parts) >= 3:
        rest = [p for p in (_POSTAL.sub("", x).strip() for x in parts[1:]) if p]
        # de-duplicate repeated components ("Lagos, Lagos")
        dedup = []
        for p_ in rest:
            if not dedup or dedup[-1].lower() != p_.lower():
                dedup.append(p_)
        cands.append((", ".join(dedup), "street"))
        if len(dedup) >= 3:
            cands.append((", ".join(dedup[-3:]), "area"))
    seen, out = set(), []
    for q, acc in cands:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append((q, acc))
    return out[:3]


def forward(db: Session, text: str, city_hint: str | None = None) -> Place | None:
    """
    Free text ("Landmark Centre, Lagos", "Berlin") → Place. Never raises.
    Tries progressively simpler versions of an address before giving up, and
    records how precise the answer is.
    """
    text = (text or "").strip()
    if not text:
        return None
    for i, (query, cap) in enumerate(forward_candidates(text, city_hint)):
        key = _key("fwd2", query.lower())
        if _cache_has(db, key):
            place = _cache_get(db, key)
        else:
            place, answered = _chain(_google_forward, _provider_forward, query)
            if not answered:
                break   # providers down — go to the gazetteer
            _cache_put(db, key, query, place)
        if place is not None:
            place.accuracy = worst_accuracy(place.accuracy, cap)
            return place
        if i == 0 and not (_google_available() or _provider_available()):
            break
    return gazetteer_lookup(text) or (gazetteer_lookup(city_hint) if city_hint else None)


def reset_breaker() -> None:
    """For tests."""
    global _provider_down_until, _google_down_until, _last_call
    _provider_down_until = 0.0
    _google_down_until = 0.0
    _last_call = 0.0
