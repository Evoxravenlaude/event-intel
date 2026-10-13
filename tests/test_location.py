"""
Location accuracy layer: Google/Nominatim geocoding, metro-aware labels,
progressive address fallback, place search, IP fallback, public-read access,
scan rate limiting, distances, venue identity.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services import geocoding, ip_geo, places, ratelimit


@pytest.fixture(autouse=True)
def _reset():
    geocoding.reset_breaker(); places.reset(); ratelimit.reset(); ip_geo._cache.clear()
    yield
    geocoding.reset_breaker(); places.reset(); ratelimit.reset(); ip_geo._cache.clear()


def _mock(monkeypatch, module, attr, handler, **kw):
    monkeypatch.setattr(module, attr, lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handler), **kw))


# ---------------------------------------------------------------------------
# Nominatim: Lagos has no `city` key — the label must be the metro, not the LGA
# ---------------------------------------------------------------------------

NOMINATIM_VI = {
    "lat": "6.4305", "lon": "3.4100", "place_rank": 18, "display_name": "Adeola Odeku Street, Victoria Island, Eti Osa, Lagos State, Nigeria",
    "address": {"road": "Adeola Odeku Street", "suburb": "Victoria Island", "county": "Eti Osa",
                "state": "Lagos State", "country": "Nigeria", "country_code": "ng"},
}


@pytest.fixture()
def nominatim_on(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", True)
    monkeypatch.setattr(cfg.settings, "geocoder_min_interval_seconds", 0.0)
    calls = []

    def handler(req):
        calls.append(req.url)
        if req.url.path.endswith("/reverse"):
            assert req.url.params["zoom"] == "16"
            return httpx.Response(200, json=NOMINATIM_VI)
        q = req.url.params.get("q", "")
        if q.startswith("1 Adeola Odeku"):
            return httpx.Response(200, json=[{**NOMINATIM_VI, "place_rank": 26}])
        return httpx.Response(200, json=[])

    _mock(monkeypatch, geocoding, "_client", handler, base_url="https://nominatim.test")
    return calls


def test_lagos_label_is_metro_not_lga(db_session, nominatim_on):
    p = geocoding.reverse(db_session, 6.4305, 3.4100)
    assert p.city == "Lagos" and p.neighbourhood == "Victoria Island" and p.country_code == "NG"
    assert p.label == "Victoria Island, Lagos"
    assert p.source == "nominatim" and p.accuracy == "area"


def test_progressive_forward_falls_back_and_caps_accuracy(db_session, nominatim_on):
    p = geocoding.forward(db_session, "Ilé MIMU, 1 Adeola Odeku St, Victoria Island, Lagos 106104, Lagos, Nigeria")
    assert p is not None and p.accuracy == "street"          # 2nd candidate, capped at street
    queries = [c.params.get("q") for c in nominatim_on if c.path.endswith("/search")]
    assert queries[0].startswith("Ilé MIMU") and queries[1] == "1 Adeola Odeku St, Victoria Island, Lagos, Nigeria"
    # cached: no further provider calls
    n = len(nominatim_on)
    geocoding.forward(db_session, "Ilé MIMU, 1 Adeola Odeku St, Victoria Island, Lagos 106104, Lagos, Nigeria")
    assert len(nominatim_on) == n


# ---------------------------------------------------------------------------
# Google Geocoding
# ---------------------------------------------------------------------------

GOOGLE_REVERSE = {"status": "OK", "results": [
    {"formatted_address": "1 Adeola Odeku St, Victoria Island, Lagos 106104, Lagos, Nigeria",
     "geometry": {"location": {"lat": 6.4306, "lng": 3.4101}, "location_type": "ROOFTOP"},
     "types": ["street_address"],
     "address_components": [
         {"long_name": "Adeola Odeku Street", "short_name": "Adeola Odeku St", "types": ["route"]},
         {"long_name": "Victoria Island", "short_name": "VI", "types": ["sublocality_level_1", "sublocality", "political"]},
         {"long_name": "Eti-Osa", "short_name": "Eti-Osa", "types": ["administrative_area_level_2", "political"]},
         {"long_name": "Lagos", "short_name": "LA", "types": ["administrative_area_level_1", "political"]},
         {"long_name": "Nigeria", "short_name": "NG", "types": ["country", "political"]}]},
    {"formatted_address": "Lagos, Nigeria", "geometry": {"location": {"lat": 6.52, "lng": 3.37}, "location_type": "APPROXIMATE"},
     "types": ["locality"], "address_components": [
         {"long_name": "Lagos", "short_name": "Lagos", "types": ["locality", "political"]},
         {"long_name": "Nigeria", "short_name": "NG", "types": ["country", "political"]}]},
]}


@pytest.fixture()
def google_on(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "google_maps_api_key", "srv-key")
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", True)
    state = {"calls": [], "status": "OK"}

    def handler(req):
        state["calls"].append(req.url)
        assert req.url.params["key"] == "srv-key"
        if state["status"] != "OK":
            return httpx.Response(200, json={"status": state["status"], "error_message": "nope"})
        if "latlng" in req.url.params:
            return httpx.Response(200, json=GOOGLE_REVERSE)
        if "Unknown" in req.url.params["address"]:
            return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
        return httpx.Response(200, json={"status": "OK", "results": [GOOGLE_REVERSE["results"][0]]})

    _mock(monkeypatch, geocoding, "_google_client", handler)
    return state


def test_google_reverse_borrows_locality_and_neighbourhood(db_session, google_on):
    p = geocoding.reverse(db_session, 6.4306, 3.4101)
    assert p.source == "google" and p.accuracy == "exact"
    assert p.city == "Lagos" and p.neighbourhood == "Victoria Island" and p.country_code == "NG"
    assert p.label == "Victoria Island, Lagos"


def test_google_forward_exact_and_zero_results_is_an_answer(db_session, google_on, nominatim_on):
    p = geocoding.forward(db_session, "1 Adeola Odeku St, Victoria Island, Lagos")
    assert p.source == "google" and p.accuracy == "exact" and p.latitude == pytest.approx(6.4306)
    assert geocoding.forward(db_session, "Unknown Place") is None
    assert not any(c.path.endswith("/search") for c in nominatim_on)   # Google answered; Nominatim untouched


def test_google_error_falls_through_to_nominatim(db_session, google_on, nominatim_on):
    google_on["status"] = "REQUEST_DENIED"
    p = geocoding.reverse(db_session, 6.4305, 3.4100)
    assert p.source == "nominatim" and p.city == "Lagos"
    assert geocoding._google_available() is False                      # breaker tripped


# ---------------------------------------------------------------------------
# Place search
# ---------------------------------------------------------------------------

def test_places_google_autocomplete_and_resolve(db_session, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "google_maps_api_key", "srv-key")
    seen = {}

    def handler(req):
        assert req.headers["X-Goog-Api-Key"] == "srv-key"
        if req.url.path.endswith("places:autocomplete"):
            import json
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"suggestions": [{"placePrediction": {
                "placeId": "ChIJvi", "text": {"text": "Victoria Island, Lagos, Nigeria"},
                "structuredFormat": {"mainText": {"text": "Victoria Island"}, "secondaryText": {"text": "Lagos, Nigeria"}},
                "types": ["sublocality_level_1", "sublocality", "political"]}}]})
        assert req.headers["X-Goog-FieldMask"].startswith("id,displayName")
        return httpx.Response(200, json={
            "id": "ChIJvi", "displayName": {"text": "Victoria Island"}, "formattedAddress": "Victoria Island, Lagos, Nigeria",
            "location": {"latitude": 6.4281, "longitude": 3.4219}, "types": ["sublocality_level_1", "political"],
            "addressComponents": [
                {"longText": "Victoria Island", "shortText": "VI", "types": ["sublocality_level_1", "sublocality", "political"]},
                {"longText": "Lagos", "shortText": "Lagos", "types": ["locality", "political"]},
                {"longText": "Nigeria", "shortText": "NG", "types": ["country", "political"]}]})

    _mock(monkeypatch, places, "_client", handler)
    sugg = places.autocomplete("victoria isl", lat=6.5, lng=3.4, session="tok-1")
    assert sugg[0].id == "google:ChIJvi" and sugg[0].primary == "Victoria Island" and sugg[0].kind == "area"
    assert seen["body"]["sessionToken"] == "tok-1" and "locationBias" in seen["body"]
    p = places.resolve(db_session, "google:ChIJvi", session="tok-1")
    assert p.latitude == pytest.approx(6.4281) and p.city == "Lagos" and p.accuracy == "area"
    assert p.label == "Victoria Island, Lagos"


def test_places_photon_fallback(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "google_maps_api_key", None)
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", True)

    def handler(req):
        assert req.url.params["q"] == "liber"
        return httpx.Response(200, json={"features": [{"geometry": {"coordinates": [9.4542, 0.3901]},
            "properties": {"name": "Libreville", "type": "city", "state": "Estuaire", "country": "Gabon",
                           "osm_type": "R", "osm_id": 2803765}}]})

    _mock(monkeypatch, places, "_client", handler)
    s = places.autocomplete("liber")
    assert s[0].primary == "Libreville" and s[0].latitude == pytest.approx(0.3901) and s[0].kind == "city"
    assert s[0].secondary == "Estuaire, Gabon"


def test_places_gazetteer_when_offline(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "google_maps_api_key", None)
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", False)
    s = places.autocomplete("nai")
    assert s[0].primary == "Nairobi" and s[0].latitude is not None


def test_places_endpoints(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "google_maps_api_key", None)
    r = client.get("/places/autocomplete?q=lag").json()
    assert r[0]["primary"] == "Lagos" and r[0]["provider"] == "gazetteer"
    r = client.get(f"/places/resolve?id={r[0]['id']}").json()
    assert r["city"] == "Lagos" and r["label"] == "Lagos, Nigeria"
    assert client.get("/places/resolve?id=gazetteer:Atlantis").status_code == 404


# ---------------------------------------------------------------------------
# IP fallback
# ---------------------------------------------------------------------------

def test_client_ip_skips_private_hops():
    class R:
        headers = {"x-forwarded-for": "10.0.0.5, 102.89.34.12, 100.64.0.1"}
        client = type("C", (), {"host": "127.0.0.1"})()
    assert ip_geo.client_ip(R()) == "102.89.34.12"


def test_geo_ip_endpoint(client, monkeypatch):
    def handler(req):
        assert "102.89.34.12" in str(req.url)
        return httpx.Response(200, json={"latitude": 6.45, "longitude": 3.39, "city": "Lagos",
                                         "country_name": "Nigeria", "country_code": "NG"})
    real_client = httpx.Client
    monkeypatch.setattr(ip_geo.httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    r = client.get("/geo/ip", headers={"X-Forwarded-For": "102.89.34.12"})
    assert r.status_code == 200
    d = r.json()
    assert d["city"] == "Lagos" and d["accuracy"] == "city" and d["source"] == "ip"
    assert client.get("/geo/ip", headers={"X-Forwarded-For": "10.1.1.1"}).status_code == 404


# ---------------------------------------------------------------------------
# /config + public read + rate limit
# ---------------------------------------------------------------------------

def test_config_reports_map_provider(client, monkeypatch):
    from app.core import config as cfg
    d = client.get("/config").json()
    assert d["map_provider"] == "maplibre" and d["map_style_url"].startswith("https://")
    assert d["google_maps_browser_key"] is None
    monkeypatch.setattr(cfg.settings, "google_maps_browser_key", "browser-key")
    monkeypatch.setattr(cfg.settings, "google_maps_api_key", "srv-key")
    d = client.get("/config").json()
    assert d["map_provider"] == "google" and d["google_maps_browser_key"] == "browser-key"
    assert d["place_search_provider"] == "google" and d["geocoder"] == "google"
    assert "srv-key" not in str(d)          # the server key is never exposed


def test_public_read_with_api_key_set(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    for path in ["/discover?lat=6.45&lng=3.39&scan=false", "/events", "/config", "/stats", "/places/autocomplete?q=lag"]:
        assert client.get(path).status_code == 200, path
    assert client.post("/events", json={"title": "x"}).status_code == 401
    assert client.get("/signals").status_code == 401
    assert client.get("/review-queue").status_code == 401
    assert client.get("/discover/coverage").status_code == 401
    monkeypatch.setattr(cfg.settings, "public_read", False)
    assert client.get("/events").status_code == 401


def test_anonymous_scans_are_rate_limited(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    monkeypatch.setattr(cfg.settings, "public_scans_per_hour", 2)
    monkeypatch.setattr(cfg.settings, "ticketmaster_api_key", None)
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    h = {"X-Forwarded-For": "102.89.34.12"}
    reasons = [client.get("/discover?lat=6.45&lng=3.39&refresh=true", headers=h).json()["coverage"]["reason"] for _ in range(3)]
    assert reasons[:2] == ["forced", "forced"] and reasons[2] == "rate_limited"
    # another visitor is unaffected; admin is never limited
    assert client.get("/discover?lat=6.45&lng=3.39&refresh=true", headers={"X-Forwarded-For": "41.58.1.1"}).json()["coverage"]["reason"] == "forced"
    assert client.get("/discover?lat=6.45&lng=3.39&refresh=true", headers={**h, "X-API-Key": "k"}).json()["coverage"]["reason"] == "forced"


# ---------------------------------------------------------------------------
# Distances + sorting
# ---------------------------------------------------------------------------

def _sig(client, ext, title, lat, lng, days, loc="Some Venue"):
    client.post("/signals", json={"source_type": "luma", "external_id": ext, "title": title, "location_text": loc,
                                  "latitude": lat, "longitude": lng, "source_confidence": 0.86,
                                  "detected_start_time": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()})


def test_discover_returns_distances_and_sorts_by_distance(client):
    _sig(client, "a", "Far but soon quartz", 6.60, 3.35, 1, "Ikeja Hall")        # ~15 km
    _sig(client, "b", "Near but later maple", 6.4310, 3.4105, 5, "VI Lounge")     # ~0.1 km
    client.post("/signals/cluster")
    d = client.get("/discover?lat=6.4305&lng=3.4100&radius_km=25&scan=false").json()
    assert [e["title"] for e in d["items"]] == ["Far but soon quartz", "Near but later maple"]   # time sort
    assert d["items"][1]["distance_km"] < 0.2 and 14 < d["items"][0]["distance_km"] < 20
    d = client.get("/discover?lat=6.4305&lng=3.4100&radius_km=25&scan=false&sort=distance").json()
    assert [e["title"] for e in d["items"]] == ["Near but later maple", "Far but soon quartz"] and d["sort"] == "distance"
    assert d["items"][0]["venue"]["geo_accuracy"] == "exact"


# ---------------------------------------------------------------------------
# Venue identity — the "everything stacked on one pin" bug
# ---------------------------------------------------------------------------

def test_hidden_address_events_keep_their_own_coordinates(client):
    """Luma sends LOCATION=<url> (→ city fallback "Lagos") but exact GEO per event."""
    _sig(client, "h1", "Hidden one quartz", 6.4253, 3.4458, 2, loc="Lagos")
    _sig(client, "h2", "Hidden two maple", 6.5063, 3.3745, 3, loc="Lagos")
    client.post("/signals/cluster")
    evs = {e["title"]: e["venue"] for e in client.get("/events").json()["items"]}
    a, b = evs["Hidden one quartz"], evs["Hidden two maple"]
    assert a["id"] != b["id"]
    assert a["latitude"] == pytest.approx(6.4253) and b["latitude"] == pytest.approx(6.5063)


def test_same_named_venue_merges_only_when_close(client):
    a = client.post("/events", json={"title": "A", "venue": {"name": "The Garden", "latitude": 6.4300, "longitude": 3.4200}}).json()["venue"]
    b = client.post("/events", json={"title": "B", "venue": {"name": "The Garden", "latitude": 6.4310, "longitude": 3.4205}}).json()["venue"]  # ~120 m
    c = client.post("/events", json={"title": "C", "venue": {"name": "The Garden", "latitude": 6.4500, "longitude": 3.4200}}).json()["venue"]  # ~2.2 km
    assert a["id"] == b["id"] and a["id"] != c["id"]


def test_coordinate_only_signal_gets_a_venue(client):
    client.post("/signals", json={"source_type": "x", "external_id": "c1", "title": "Pop-up quartz night",
                                  "latitude": 6.4305, "longitude": 3.4100})
    ev_id = client.post("/signals/cluster").json()["created_event_ids"][0]
    v = client.get(f"/events/{ev_id}").json()["venue"]
    assert v["latitude"] == pytest.approx(6.4305) and v["city"] == "Lagos"


# ---------------------------------------------------------------------------
# Scheduler geo jobs record coverage
# ---------------------------------------------------------------------------

def test_scheduler_geo_job_marks_area_fresh(client, db_engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.core import config as cfg
    from app.workers import scheduler
    monkeypatch.setattr(cfg.settings, "ticketmaster_api_key", None)   # mocks enabled → 1 mock item
    monkeypatch.setattr(scheduler, "SessionLocal", sessionmaker(bind=db_engine))
    out = scheduler.run_ingest_job("ticketmaster", None, None, None, 6.45, 3.39, 30)
    assert out["status"] == "completed"
    d = client.get("/discover?lat=6.46&lng=3.40&radius_km=10&scan=false").json()
    assert d["coverage"]["reason"] == "fresh"


# ---------------------------------------------------------------------------
# PWA shell + "view original listing" link
# ---------------------------------------------------------------------------

def test_manifest_and_icons_are_public(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and "manifest" in r.headers["content-type"]
    m = r.json()
    assert m["display"] == "standalone" and m["start_url"]
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    assert client.get("/").status_code == 200


def test_event_exposes_source_url(client):
    when = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    r = client.post("/signals", json={
        "source_type": "luma", "external_id": "evt-src-1", "title": "Rooftop Listening Session",
        "url": "https://lu.ma/rooftop-listening", "location_text": "Ilé MIMU, Lagos",
        "latitude": 6.4305, "longitude": 3.4215, "detected_start_time": when, "source_confidence": 0.9,
    })
    assert r.status_code == 201
    assert client.post("/signals/cluster").status_code == 200
    items = client.get("/events").json()["items"]
    ev = next(e for e in items if e["title"] == "Rooftop Listening Session")
    assert ev["source_url"] == "https://lu.ma/rooftop-listening"
    assert client.get(f"/events/{ev['id']}").json()["source_url"] == "https://lu.ma/rooftop-listening"


# ---------------------------------------------------------------------------
# Map provider choice + Apple MapKit tokens
# ---------------------------------------------------------------------------

@pytest.fixture()
def mapkit_on(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from app.core import config as cfg
    from app.services import mapkit
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    monkeypatch.setattr(cfg.settings, "apple_mapkit_team_id", "TEAM123456")
    monkeypatch.setattr(cfg.settings, "apple_mapkit_key_id", "KEY7654321")
    monkeypatch.setattr(cfg.settings, "apple_mapkit_private_key", pem.replace("\n", "\\n"))   # as pasted into an env var
    monkeypatch.setattr(cfg.settings, "apple_mapkit_origin", "https://pulse.example")
    mapkit.reset()
    yield key.public_key()
    mapkit.reset()


def test_map_provider_resolution(monkeypatch, mapkit_on):
    from app.core import config as cfg
    s = cfg.settings
    assert s.resolved_map_provider == "apple"                 # auto: MapKit configured, no Google browser key
    monkeypatch.setattr(s, "google_maps_browser_key", "bk")
    assert s.resolved_map_provider == "google"                # auto prefers an explicit Google key
    monkeypatch.setattr(s, "map_provider", "apple")
    assert s.resolved_map_provider == "apple"
    monkeypatch.setattr(s, "map_provider", "maplibre")
    assert s.resolved_map_provider == "maplibre"
    monkeypatch.setattr(s, "map_provider", "google")
    monkeypatch.setattr(s, "google_maps_browser_key", None)
    assert s.resolved_map_provider == "apple"                 # asked for google without a key → next best


def test_mapkit_token_is_valid_es256(client, mapkit_on):
    import jwt
    r = client.get("/mapkit/token")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    tok = r.text
    hdr = jwt.get_unverified_header(tok)
    assert hdr["alg"] == "ES256" and hdr["kid"] == "KEY7654321"
    claims = jwt.decode(tok, mapkit_on, algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456" and claims["origin"] == "https://pulse.example"
    assert 300 <= claims["exp"] - claims["iat"] <= 3600
    assert client.get("/mapkit/token").text == tok           # cached until near expiry
    assert client.get("/config").json()["map_provider"] == "apple"


def test_mapkit_token_404_when_not_configured(client):
    assert client.get("/mapkit/token").status_code == 404
