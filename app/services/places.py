"""
Place search for the location picker ("type a city, area or venue").

Provider chain:
  1. Google Places API (New) — autocomplete + details. Best coverage everywhere,
     including Lagos/Nairobi neighbourhoods and venues. Needs GOOGLE_MAPS_API_KEY
     with "Places API (New)" enabled. Session tokens group an autocomplete
     session + its details call into one billable session.
  2. Photon (komoot, OpenStreetMap) — free, built for type-ahead. Nominatim's
     usage policy forbids autocomplete, so it is never used here.
  3. Built-in gazetteer prefix match — always works, offline.

Suggestions carry coordinates when the provider gives them (Photon,
gazetteer); Google suggestions need a details call (`resolve`).
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, asdict

import httpx

from app.core.config import settings
from app.services import geocoding
from app.services.geocoding import Place

logger = logging.getLogger(__name__)

_google_down_until = 0.0
_photon_down_until = 0.0


@dataclass
class Suggestion:
    id: str                      # "google:<placeId>" | "photon:<osm>" | "gazetteer:<name>"
    primary: str                 # "Victoria Island"
    secondary: str | None        # "Lagos, Nigeria"
    provider: str
    latitude: float | None = None
    longitude: float | None = None
    kind: str | None = None      # city | area | venue | address

    def to_dict(self) -> dict:
        return asdict(self)


def _down(which: str, exc: Exception) -> None:
    global _google_down_until, _photon_down_until
    until = time.monotonic() + 120
    if which == "google":
        _google_down_until = until
    else:
        _photon_down_until = until
    logger.warning("Place search provider %s failing (%s) — skipping for 120s", which, exc)


def _client() -> httpx.Client:
    return httpx.Client(timeout=6, headers={"User-Agent": settings.geocoder_user_agent})


# ---------------------------------------------------------------------------
# Google Places API (New)
# ---------------------------------------------------------------------------

_GOOGLE_KIND = {
    "locality": "city", "administrative_area_level_1": "city", "administrative_area_level_2": "city",
    "sublocality": "area", "sublocality_level_1": "area", "neighborhood": "area",
    "route": "address", "street_address": "address", "premise": "address",
}


def _google_autocomplete(q: str, lat: float | None, lng: float | None, session: str | None) -> list[Suggestion]:
    body: dict = {"input": q, "languageCode": "en"}
    if lat is not None and lng is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 50000.0}}
    if session:
        body["sessionToken"] = session
    with _client() as c:
        r = c.post(f"{settings.google_places_url.rstrip('/')}/places:autocomplete", json=body,
                   headers={"X-Goog-Api-Key": settings.google_maps_api_key})
        r.raise_for_status()
        data = r.json()
    out = []
    for s in data.get("suggestions") or []:
        pp = s.get("placePrediction")
        if not pp:
            continue
        fmt = pp.get("structuredFormat") or {}
        primary = ((fmt.get("mainText") or {}).get("text")) or ((pp.get("text") or {}).get("text")) or ""
        secondary = (fmt.get("secondaryText") or {}).get("text")
        types = pp.get("types") or []
        kind = next((_GOOGLE_KIND[t] for t in types if t in _GOOGLE_KIND), "venue" if "establishment" in types else None)
        out.append(Suggestion(id=f"google:{pp.get('placeId')}", primary=primary, secondary=secondary,
                              provider="google", kind=kind))
    return out


def _google_details(place_id: str, session: str | None) -> Place | None:
    params = {"languageCode": "en"}
    if session:
        params["sessionToken"] = session
    with _client() as c:
        r = c.get(f"{settings.google_places_url.rstrip('/')}/places/{place_id}", params=params, headers={
            "X-Goog-Api-Key": settings.google_maps_api_key,
            "X-Goog-FieldMask": "id,displayName,formattedAddress,location,addressComponents,types,viewport",
        })
        r.raise_for_status()
        d = r.json()
    loc = d.get("location") or {}
    if "latitude" not in loc:
        return None
    comps = geocoding.parse_google_components(d.get("addressComponents") or [], long_key="longText", short_key="shortText")
    types = set(d.get("types") or [])
    if types & {"locality", "administrative_area_level_1", "administrative_area_level_2", "country"}:
        accuracy = "city"
    elif types & {"sublocality", "sublocality_level_1", "neighborhood", "postal_code"}:
        accuracy = "area"
    else:
        accuracy = "exact"
    place = geocoding.place_from_google_components(
        float(loc["latitude"]), float(loc["longitude"]), comps, accuracy=accuracy,
        display_name=d.get("formattedAddress"),
    )
    # For a venue, the venue name is the most useful "neighbourhood"-level label
    name = (d.get("displayName") or {}).get("text")
    if accuracy == "exact" and name:
        place.neighbourhood = name
    return place


# ---------------------------------------------------------------------------
# Photon
# ---------------------------------------------------------------------------

_PHOTON_KIND = {"city": "city", "town": "city", "village": "city", "state": "city", "county": "city",
                "district": "area", "suburb": "area", "locality": "area", "neighbourhood": "area",
                "street": "address", "house": "address"}


def _photon(q: str, lat: float | None, lng: float | None) -> list[Suggestion]:
    params: dict = {"q": q, "limit": 6, "lang": "en"}
    if lat is not None and lng is not None:
        params.update({"lat": lat, "lon": lng})
    with _client() as c:
        r = c.get(settings.photon_url, params=params)
        r.raise_for_status()
        data = r.json()
    out = []
    for f in data.get("features") or []:
        pr = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) != 2:
            continue
        name = pr.get("name") or pr.get("street") or pr.get("city")
        if not name:
            continue
        sec = [x for x in (pr.get("city") if pr.get("city") != name else None, pr.get("state"), pr.get("country")) if x]
        out.append(Suggestion(
            id=f"photon:{pr.get('osm_type', '')}{pr.get('osm_id', '')}", primary=name,
            secondary=", ".join(dict.fromkeys(sec)) or None, provider="photon",
            latitude=float(coords[1]), longitude=float(coords[0]),
            kind=_PHOTON_KIND.get(pr.get("type") or "", "venue" if pr.get("osm_key") in ("amenity", "leisure", "tourism", "shop") else None),
        ))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def autocomplete(q: str, lat: float | None = None, lng: float | None = None, session: str | None = None) -> list[Suggestion]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    if settings.google_maps_api_key and time.monotonic() >= _google_down_until:
        try:
            return _google_autocomplete(q, lat, lng, session)
        except Exception as exc:
            _down("google", exc)
    if settings.geocoding_enabled and time.monotonic() >= _photon_down_until:
        try:
            res = _photon(q, lat, lng)
            if res:
                return res
        except Exception as exc:
            _down("photon", exc)
    return [Suggestion(id=f"gazetteer:{p.city}", primary=p.city, secondary=p.country, provider="gazetteer",
                       latitude=p.latitude, longitude=p.longitude, kind="city")
            for p in geocoding.gazetteer_prefix(q)]


def resolve(db, suggestion_id: str, session: str | None = None) -> Place | None:
    """Suggestion id → Place with coordinates and labels."""
    provider, _, ref = suggestion_id.partition(":")
    if provider == "google" and settings.google_maps_api_key:
        try:
            return _google_details(ref, session)
        except Exception as exc:
            _down("google", exc)
            return None
    if provider == "gazetteer":
        return geocoding.gazetteer_lookup(ref)
    return None


def reset() -> None:
    global _google_down_until, _photon_down_until
    _google_down_until = _photon_down_until = 0.0
