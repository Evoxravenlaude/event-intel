"""
Location endpoints for the client app.

GET /config                       what the frontend should load (map provider, keys meant for browsers, limits)
GET /places/autocomplete?q=       place search type-ahead (Google Places → Photon → gazetteer)
GET /places/resolve?id=           suggestion → coordinates + labels
GET /geo/ip                       approximate location from the caller's IP (GPS denied / unavailable)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.discover import PlaceOut
from app.core.config import settings
from app.db.session import get_db
from app.services import ip_geo, places

router = APIRouter(tags=["location"])


class ConfigOut(BaseModel):
    version: str
    map_provider: str                      # google | leaflet
    google_maps_browser_key: str | None    # referrer-restricted browser key, safe to expose
    place_search_provider: str             # google | photon | gazetteer
    geocoder: str                          # google | nominatim | gazetteer
    discover_default_radius_km: float
    discover_max_radius_km: float
    public_read: bool


class SuggestionOut(BaseModel):
    id: str
    primary: str
    secondary: str | None = None
    provider: str
    latitude: float | None = None
    longitude: float | None = None
    kind: str | None = None


@router.get("/config", response_model=ConfigOut)
def get_config(request: Request):
    from app.main import app
    return ConfigOut(
        version=app.version,
        map_provider="google" if settings.google_maps_browser_key else "leaflet",
        google_maps_browser_key=settings.google_maps_browser_key,
        place_search_provider="google" if settings.google_maps_api_key else ("photon" if settings.geocoding_enabled else "gazetteer"),
        geocoder="google" if settings.google_maps_api_key else ("nominatim" if settings.geocoding_enabled else "gazetteer"),
        discover_default_radius_km=settings.discover_default_radius_km,
        discover_max_radius_km=settings.discover_max_radius_km,
        public_read=settings.public_read,
    )


@router.get("/places/autocomplete", response_model=list[SuggestionOut])
def places_autocomplete(
    q: str = Query(min_length=1, max_length=120),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    session: str | None = Query(default=None, max_length=64, description="client-generated token per search session"),
):
    return [SuggestionOut(**s.to_dict()) for s in places.autocomplete(q, lat, lng, session)]


@router.get("/places/resolve", response_model=PlaceOut)
def places_resolve(
    id: str = Query(min_length=3, max_length=300),
    session: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
):
    place = places.resolve(db, id, session)
    if place is None:
        raise HTTPException(status_code=404, detail="Place not found")
    return PlaceOut(**place.to_dict())


@router.get("/geo/ip", response_model=PlaceOut)
def geo_from_ip(request: Request):
    place = ip_geo.locate(ip_geo.client_ip(request))
    if place is None:
        raise HTTPException(status_code=404, detail="Could not determine location from IP")
    return PlaceOut(**place.to_dict())
