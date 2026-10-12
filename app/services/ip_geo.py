"""
Approximate location from the visitor's IP — used only when the browser
can't or won't give a GPS fix. City-level at best; labelled as such.
"""
from __future__ import annotations
import ipaddress
import logging
import time

import httpx
from fastapi import Request

from app.core.config import settings
from app.services.geocoding import Place

logger = logging.getLogger(__name__)
_cache: dict[str, tuple[float, Place | None]] = {}
_TTL = 6 * 3600


def client_ip(request: Request) -> str | None:
    """First public address in X-Forwarded-For (Railway/most proxies), else the socket peer."""
    candidates = []
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        candidates.extend(x.strip() for x in fwd.split(","))
    real = request.headers.get("x-real-ip")
    if real:
        candidates.append(real.strip())
    if request.client:
        candidates.append(request.client.host)
    for c in candidates:
        try:
            ip = ipaddress.ip_address(c)
        except ValueError:
            continue
        if ip.is_global:
            return str(ip)
    return None


def locate(ip: str | None) -> Place | None:
    if not ip or not settings.ip_geo_enabled:
        return None
    hit = _cache.get(ip)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    place = None
    try:
        with httpx.Client(timeout=4, headers={"User-Agent": settings.geocoder_user_agent}) as c:
            r = c.get(settings.ip_geo_url.format(ip=ip))
            r.raise_for_status()
            d = r.json()
        lat = d.get("latitude", d.get("lat"))
        lng = d.get("longitude", d.get("lon"))
        if lat is not None and lng is not None and not d.get("error"):
            place = Place(
                latitude=float(lat), longitude=float(lng), city=d.get("city"),
                country=d.get("country_name") or d.get("country"),
                country_code=(d.get("country_code") or d.get("countryCode") or "").upper() or None,
                source="ip", accuracy="city",
            )
    except Exception as exc:
        logger.info("IP geolocation failed for %s: %s", ip, exc)
    _cache[ip] = (time.monotonic(), place)
    if len(_cache) > 5000:
        _cache.clear()
    return place
