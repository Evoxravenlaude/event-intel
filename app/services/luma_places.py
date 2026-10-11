"""
Luma city ("discover place") directory.

Luma runs a public, curated events page for ~80 cities worldwide (lu.ma/lagos,
lu.ma/nairobi, lu.ma/london, …) and each one exposes an iCal feed:

    https://api.lu.ma/ics/get?entity=discover&id=<discplace-…>

That makes Luma a keyless, global, geo-capable source: for any point on Earth,
find the Luma cities within reach and pull their feeds.

The directory is fetched live from Luma's public bootstrap endpoint and cached
in-process for 24 h; if that fails, the built-in snapshot below (Oct 2026) is
used so discovery never depends on it.
"""
from __future__ import annotations
import logging
import threading
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.services.geo import haversine_km

logger = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://api.lu.ma/discover/bootstrap-page"
CACHE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class LumaPlace:
    slug: str
    name: str
    api_id: str
    lat: float
    lng: float

    @property
    def ics_url(self) -> str:
        return f"{settings.luma_api_base_url.rstrip('/')}/ics/get?entity=discover&id={self.api_id}"

    @property
    def page_url(self) -> str:
        return f"https://luma.com/{self.slug}"


# Snapshot of api.lu.ma/discover/bootstrap-page (2026-10-10). slug, name, api_id, lat, lng
_SNAPSHOT: list[tuple[str, str, str, float, float]] = [
    ("cincinnati", "Cincinnati", "discplace-WdZuTYoQlHeu403", 39.1413, -84.506),
    ("pittsburgh", "Pittsburgh", "discplace-Dt211oc2OdbLhnS", 40.4397, -79.9763),
    ("detroit", "Detroit", "discplace-nkE8zpyEW5OBLlE", 42.3834, -83.1024),
    ("waterloo_ca", "Waterloo", "discplace-idpnif8MiNuyYI7", 43.4643, -80.5204),
    ("chicago", "Chicago", "discplace-NdGm35qFD0vaXNF", 41.8375, -87.6866),
    ("toronto", "Toronto", "discplace-Cx3JMS6vXKAbhV5", 43.65107, -79.347015),
    ("dc", "Washington, DC", "discplace-AANPgOymN6bqFn8", 38.9047, -77.0163),
    ("raleigh", "Raleigh", "discplace-gpqryTpxaqtM41R", 35.8324, -78.6429),
    ("philadelphia", "Philadelphia", "discplace-VGLZZfVwOKRD1Yd", 40.0077, -75.1339),
    ("atlanta", "Atlanta", "discplace-C6hWuH5suHJIUqC", 33.7628, -84.422),
    ("nyc", "New York", "discplace-Izx1rQVSh8njYpP", 40.73061, -73.935242),
    ("montreal", "Montréal", "discplace-CXKKcJmNkbj6ikW", 45.5089, -73.5617),
    ("minneapolis", "Minneapolis", "discplace-IHi0OqR5c6t4Hb3", 44.9635, -93.2678),
    ("boston", "Boston", "discplace-VWeZ1zUvnawYHMj", 42.3188, -71.0852),
    ("dallas", "Dallas", "discplace-Ez9iuaZfs6AZDls", 32.7935, -96.7667),
    ("houston", "Houston", "discplace-aQeJaEtqg3shHZ1", 29.786, -95.3885),
    ("miami", "Miami", "discplace-fSrrRYurTwydAGK", 25.784, -80.2101),
    ("austin", "Austin", "discplace-0tPy8KGz3xMycnt", 30.2672, -97.7413),
    ("denver", "Denver", "discplace-I94ZmQKKyVnCQKv", 39.762, -104.8758),
    ("salt-lake-city", "Salt Lake City", "discplace-gxZJbB572Ls8RRu", 40.7776, -111.9311),
    ("phoenix", "Phoenix", "discplace-Vk9M1gTb4AMVXuD", 33.5722, -112.0892),
    ("calgary", "Calgary", "discplace-7AxSBoZHQy3igIZ", 51.05, -114.0667),
    ("edmonton", "Edmonton", "discplace-Boo0oZU0eJQMr24", 53.5344, -113.4903),
    ("mexico-city", "Mexico City", "discplace-ntiNB0E437TyRqt", 19.4326, -99.1332),
    ("las-vegas", "Las Vegas", "discplace-RF9Yq9JDUxmcpTr", 36.2333, -115.2654),
    ("sd", "San Diego", "discplace-MNBATdzid940kqJ", 32.8313, -117.1222),
    ("la", "Los Angeles", "discplace-OgfEAh5KgfMzise", 34.052235, -118.243683),
    ("seattle", "Seattle", "discplace-FQ4E58PeBMHGTKK", 47.6061, -122.3328),
    ("portland", "Portland", "discplace-HthnjGVBzGh90sQ", 45.523064, -122.676483),
    ("sacramento", "Sacramento", "discplace-3ewzk9TI8gm2QCC", 38.5677, -121.4685),
    ("vancouver", "Vancouver", "discplace-4fa7ldlAkBTTivm", 49.2827, -123.1207),
    ("sf", "San Francisco", "discplace-BDj7GNbGlsF7Cka", 37.7749, -122.4194),
    ("medellin", "Medellín", "discplace-K11Mq0Pw6sbManZ", 6.2308, -75.5906),
    ("bogota", "Bogotá", "discplace-Rac9aE9RdKypLVS", 4.7111, -74.0722),
    ("dublin", "Dublin", "discplace-ffI8KmAB4gC5LMC", 53.35, -6.2603),
    ("lisbon", "Lisbon", "discplace-mgGFFo5EDdyekyE", 38.7253, -9.15),
    ("london", "London", "discplace-QCcNk3HXowOR97j", 51.509865, -0.118092),
    ("oslo", "Oslo", "discplace-Q5DXX8la0vuCuz2", 59.9133, 10.7389),
    ("amsterdam", "Amsterdam", "discplace-FC4SDMUVXiFtMOr", 52.3728, 4.8936),
    ("madrid", "Madrid", "discplace-03jiEcS4mvwJuDa", 40.4168, -3.70379),
    ("paris", "Paris", "discplace-NdLrh1xJfeotJZC", 48.8566, 2.3522),
    ("brussels", "Brussels", "discplace-CMxOe3Mv06uUk7l", 50.8467, 4.3525),
    ("hamburg", "Hamburg", "discplace-xZzD6rDcDK12oi7", 53.55, 10.0),
    ("copenhagen", "Copenhagen", "discplace-CmmHAjPdBSsqmJf", 55.6761, 12.5683),
    ("frankfurt", "Frankfurt", "discplace-9fJOlBTzIKNMSda", 50.1106, 8.6822),
    ("stockholm", "Stockholm", "discplace-e7EG0Ef6S2aQnvN", 59.3294, 18.0686),
    ("barcelona", "Barcelona", "discplace-WcS4REeayDPXV4n", 41.3874, 2.1686),
    ("geneva", "Geneva", "discplace-RnVxN1SH4HYTeqF", 46.2044, 6.1432),
    ("lausanne", "Lausanne", "discplace-SmrXTBH5rgPvd1h", 46.5197, 6.6323),
    ("zurich", "Zurich", "discplace-tSRc3NkTycobe0w", 47.3769, 8.5417),
    ("berlin", "Berlin", "discplace-gCfX0s3E9Hgo3rG", 52.52, 13.405),
    ("helsinki", "Helsinki", "discplace-gEii5B2Ju5KKRNH", 60.1708, 24.9375),
    ("munich", "Munich", "discplace-P00kEGGGHNLEYGe", 48.1375, 11.575),
    ("milan", "Milan", "discplace-9AyCYUvGH7xiqhh", 45.4669, 9.19),
    ("prague", "Prague", "discplace-6xx9LRci5NFgdJ5", 50.0875, 14.4214),
    ("honolulu", "Honolulu", "discplace-Ce0yAAavKebPHcB", 21.3294, -157.846),
    ("vienna", "Vienna", "discplace-3YgdIjqj7Pveid3", 48.2081, 16.3713),
    ("warsaw", "Warsaw", "discplace-PTcuEQVHuySJe8N", 52.2297, 21.0122),
    ("rome", "Rome", "discplace-CLGg2G8Q96daz0w", 41.8931, 12.4828),
    ("budapest", "Budapest", "discplace-zS3rBqHSdNGTSZB", 47.4925, 19.0514),
    ("saopaulo", "São Paulo", "discplace-AQZnCu9wl4LmOIp", -23.5558, -46.6396),
    ("rio", "Rio de Janeiro", "discplace-EWglyhh4fsHKo2F", -22.9068, -43.1729),
    ("buenos-aires", "Buenos Aires", "discplace-wX2J5xGwAJpznew", -34.5997, -58.3819),
    ("istanbul", "Istanbul", "discplace-0vKyo1D6kdT4ml6", 41.0136, 28.955),
    ("lagos", "Lagos", "discplace-ARF3ZNcu47bs56x", 6.455, 3.3841),
    ("tel-aviv", "Tel Aviv-Yafo", "discplace-fHkSoyCyugTZSbr", 32.08, 34.78),
    ("tokyo", "Tokyo", "discplace-9H7asQEvWiv6DA9", 35.6764, 139.65),
    ("osaka", "Osaka", "discplace-jiWYrxy3DvINpjv", 34.6939, 135.5022),
    ("seoul", "Seoul", "discplace-eQieweHXBFCWbCj", 37.5519, 126.9918),
    ("dubai", "Dubai", "discplace-d3kg1aLIJ5ROF6S", 25.2048, 55.2708),
    ("new-delhi", "New Delhi", "discplace-CzipmKodUYN2Dfx", 28.61, 77.23),
    ("taipei", "Taipei", "discplace-fi7MDZq99wfKWfa", 25.0375, 121.5625),
    ("nairobi", "Nairobi", "discplace-YSx1DPerjjIyq7M", -1.2864, 36.8172),
    ("hongkong", "Hong Kong", "discplace-z9B5Guglh2WINA1", 22.3, 114.2),
    ("mumbai", "Mumbai", "discplace-Q5hkYsjZs1ZDJcU", 19.0761, 72.8775),
    ("singapore", "Singapore", "discplace-mUbtdfNjfWaLQ72", 1.29027, 103.851959),
    ("sydney", "Sydney", "discplace-TPdKGPI56hGfOdi", -33.8678, 151.21),
]

SNAPSHOT: list[LumaPlace] = [LumaPlace(*row) for row in _SNAPSHOT]

_lock = threading.Lock()
_cache: list[LumaPlace] | None = None
_cache_at = 0.0


def _parse_bootstrap(payload: dict) -> list[LumaPlace]:
    out: list[LumaPlace] = []
    for wrapper in payload.get("places") or []:
        p = (wrapper or {}).get("place") or {}
        coord = p.get("coordinate") or {}
        try:
            out.append(LumaPlace(
                slug=str(p["slug"]), name=str(p["name"]), api_id=str(p["api_id"]),
                lat=float(coord["latitude"]), lng=float(coord["longitude"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_live() -> list[LumaPlace]:
    url = settings.luma_api_base_url.rstrip("/") + "/discover/bootstrap-page"
    with httpx.Client(timeout=settings.source_timeout_seconds, follow_redirects=True,
                      headers={"User-Agent": settings.geocoder_user_agent}) as c:
        r = c.get(url)
        r.raise_for_status()
        return _parse_bootstrap(r.json())


def all_places(refresh: bool = False) -> list[LumaPlace]:
    """Live directory (cached 24 h), merged over the built-in snapshot."""
    global _cache, _cache_at
    with _lock:
        if not refresh and _cache is not None and time.monotonic() - _cache_at < CACHE_SECONDS:
            return _cache
        places = {p.api_id: p for p in SNAPSHOT}
        try:
            live = _fetch_live()
            for p in live:
                places[p.api_id] = p
            logger.info("Luma place directory refreshed: %d live places", len(live))
        except Exception as exc:
            logger.warning("Luma place directory fetch failed (%s) — using built-in snapshot", exc)
        _cache = list(places.values())
        _cache_at = time.monotonic()
        return _cache


def places_near(lat: float, lng: float, radius_km: float, max_places: int = 3) -> list[tuple[LumaPlace, float]]:
    """
    Luma cities relevant to a circle. A Luma city page covers its metro area,
    so a place counts if its centre is within radius + LUMA_PLACE_REACH_KM.
    Nearest first.
    """
    reach = radius_km + settings.luma_place_reach_km
    hits = []
    for p in all_places():
        d = haversine_km(lat, lng, p.lat, p.lng)
        if d <= reach:
            hits.append((p, d))
    hits.sort(key=lambda t: t[1])
    return hits[:max_places]


def reset_cache() -> None:
    global _cache, _cache_at
    _cache, _cache_at = None, 0.0
