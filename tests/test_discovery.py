"""
Location-first discovery: geohash, geocoding (cache + offline fallback),
geo-capable adapters, /discover scan + coverage, /geocode, scheduler geo jobs.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services import geocoding
from app.services import adapters as ad
from app.services.geohash import cells_covering, decode, encode


# ---------------------------------------------------------------------------
# Geohash
# ---------------------------------------------------------------------------

def test_geohash_encode_known_values():
    assert encode(57.64911, 10.40744, 11) == "u4pruydqqvj"      # canonical test vector
    assert encode(6.5244, 3.3792, 5) == encode(6.5250, 3.3800, 5)  # same ~5km cell
    lat, lng = decode(encode(6.5244, 3.3792, 7))
    assert abs(lat - 6.5244) < 0.01 and abs(lng - 3.3792) < 0.01


def test_cells_covering_scales_with_radius_and_is_capped():
    small = cells_covering(6.52, 3.38, 3)
    big = cells_covering(6.52, 3.38, 40)
    huge = cells_covering(6.52, 3.38, 5000)
    assert 1 <= len(small) < len(big) <= len(huge) <= 25 * 25
    assert encode(6.52, 3.38, 5) in small


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def test_reverse_falls_back_to_gazetteer_when_disabled(db_session):
    place = geocoding.reverse(db_session, 6.46, 3.40)      # Lagos Island
    assert place.city == "Lagos" and place.country_code == "NG" and place.source == "gazetteer"
    assert geocoding.reverse(db_session, 0.0, -140.0) is None   # middle of the Pacific


def test_forward_gazetteer_by_name(db_session):
    assert geocoding.forward(db_session, "Nairobi").country_code == "KE"
    assert geocoding.forward(db_session, "Some Unknown Venue") is None
    assert geocoding.forward(db_session, "Some Unknown Venue", city_hint="Accra").city == "Accra"


@pytest.fixture()
def nominatim(monkeypatch):
    """Enable the provider and route it to a MockTransport."""
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", True)
    monkeypatch.setattr(cfg.settings, "geocoder_min_interval_seconds", 0.0)
    geocoding.reset_breaker()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/reverse"):
            return httpx.Response(200, json={"lat": "48.86", "lon": "2.35", "display_name": "Paris, France",
                                             "address": {"city": "Paris", "country": "France", "country_code": "fr"}})
        if "Landmark" in request.url.params.get("q", ""):
            return httpx.Response(200, json=[{"lat": "6.4281", "lon": "3.4219", "display_name": "Landmark Centre, Lagos",
                                              "address": {"city": "Lagos", "country": "Nigeria", "country_code": "ng"}}])
        return httpx.Response(200, json=[])

    monkeypatch.setattr(geocoding, "_client", lambda: httpx.Client(base_url="https://geo.test", transport=httpx.MockTransport(handler)))
    return calls


def test_reverse_uses_provider_and_caches(db_session, nominatim):
    p1 = geocoding.reverse(db_session, 48.8566, 2.3522)
    assert p1.city == "Paris" and p1.country_code == "FR" and p1.source == "geocoder"
    p2 = geocoding.reverse(db_session, 48.8567, 2.3523)      # ~10m away → same cache key
    assert p2.source == "cache" and p2.city == "Paris"
    assert len(nominatim) == 1


def test_forward_caches_misses_too(db_session, nominatim):
    assert geocoding.forward(db_session, "Landmark Centre", city_hint="Lagos").latitude == pytest.approx(6.4281)
    assert geocoding.forward(db_session, "zzz nowhere") is None
    assert geocoding.forward(db_session, "zzz nowhere") is None
    # 1 hit + 2 attempts for the miss (with and without hint... no hint given) → 2 provider calls total
    assert len(nominatim) == 2


def test_provider_failure_trips_breaker_and_falls_back(db_session, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", True)
    monkeypatch.setattr(cfg.settings, "geocoder_min_interval_seconds", 0.0)
    geocoding.reset_breaker()
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(geocoding, "_client", lambda: httpx.Client(base_url="https://geo.test", transport=httpx.MockTransport(handler)))
    p = geocoding.reverse(db_session, 6.46, 3.40)
    assert p.city == "Lagos" and p.source == "gazetteer"
    geocoding.reverse(db_session, 5.60, -0.19)
    assert len(calls) == 1          # breaker open — second call never hits the provider


def test_venue_without_coords_is_geocoded_on_create(client):
    r = client.post("/events", json={"title": "Geo me", "venue": {"name": "Impact Hub", "city": "Nairobi"}})
    v = r.json()["venue"]
    assert v["latitude"] == pytest.approx(-1.2921) and v["longitude"] == pytest.approx(36.8219)
    assert v["country"] == "Kenya"


def test_signal_with_coords_but_no_city_gets_city_label(client):
    client.post("/signals", json={"source_type": "manual", "external_id": "g1", "title": "Rooftop jazz",
                                  "location_text": "Some Rooftop", "latitude": 6.46, "longitude": 3.40})
    ev_id = client.post("/signals/cluster").json()["created_event_ids"][0]
    v = client.get(f"/events/{ev_id}").json()["venue"]
    assert v["name"] == "Some Rooftop" and v["city"] == "Lagos" and v["country_code"] == "NG"


# ---------------------------------------------------------------------------
# Geo-capable adapters
# ---------------------------------------------------------------------------

TM_PAYLOAD = {
    "_embedded": {"events": [{
        "id": "tm1", "name": "Burna Boy Live", "url": "https://tm.test/e/tm1",
        "dates": {"start": {"dateTime": "2030-06-01T19:00:00Z"}},
        "classifications": [{"segment": {"name": "Music"}, "genre": {"name": "Afrobeats"}}],
        "priceRanges": [{"min": 45.0, "currency": "GBP"}],
        "_embedded": {"venues": [{"name": "The O2", "city": {"name": "London"},
                                  "location": {"latitude": "51.5030", "longitude": "0.0032"}}]},
    }, {
        "id": "tm2", "name": "Arsenal v Chelsea", "dates": {"start": {"localDate": "2030-06-02"}},
        "classifications": [{"segment": {"name": "Sports"}}],
        "_embedded": {"venues": [{"name": "Emirates Stadium", "city": {"name": "London"},
                                  "location": {"latitude": "51.5549", "longitude": "-0.1084"}}]},
    }]},
    "page": {"totalPages": 1},
}


@pytest.fixture()
def tm_live(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "ticketmaster_api_key", "k")
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=TM_PAYLOAD)

    monkeypatch.setattr(ad.BaseAdapter, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_ticketmaster_geo_query(tm_live):
    r = ad.TicketmasterAdapter().fetch(None, None, None, geo=ad.GeoQuery(lat=51.5, lng=-0.12, radius_km=30))
    assert tm_live["params"]["latlong"] == "51.5,-0.12" and tm_live["params"]["radius"] == "30" and tm_live["params"]["unit"] == "km"
    assert r.fetched_count == 2
    a, b = r.items
    assert a.external_id == "tm1" and a.normalized_category == "culture" and a.latitude == pytest.approx(51.503)
    assert a.location_text == "The O2, London" and "From 45.0 GBP" in a.body
    assert a.detected_start_time == datetime(2030, 6, 1, 19, tzinfo=timezone.utc)
    assert b.normalized_category == "sports" and b.detected_start_time == datetime(2030, 6, 2, tzinfo=timezone.utc)
    assert a.source_confidence > 0.9


def test_ticketmaster_without_key_mocks_or_errors(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "ticketmaster_api_key", None)
    assert ad.TicketmasterAdapter().is_configured() is False
    r = ad.TicketmasterAdapter().fetch("Lagos", "web3", None)
    assert len(r.items) == 1 and "Mock" in r.items[0].body
    monkeypatch.setattr(cfg.settings, "enable_mock_adapters", False)
    r = ad.TicketmasterAdapter().fetch("Lagos", None, None)
    assert r.items == [] and "TICKETMASTER_API_KEY" in r.errors[0]


def test_seatgeek_geo_query(monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", "cid")
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"events": [{
            "id": 77, "title": "Knicks at Nets", "type": "nba", "datetime_utc": "2030-03-03T00:30:00",
            "url": "https://sg.test/e/77", "stats": {"lowest_price": 60},
            "venue": {"name": "Barclays Center", "city": "Brooklyn", "location": {"lat": 40.6826, "lon": -73.9754}},
        }], "meta": {"total": 1, "per_page": 100}})

    monkeypatch.setattr(ad.BaseAdapter, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    r = ad.SeatGeekAdapter().fetch(None, None, None, geo=ad.GeoQuery(lat=40.7, lng=-74.0, radius_km=10))
    assert seen["params"]["range"] == "10km"
    s = r.items[0]
    assert s.normalized_category == "sports" and s.latitude == pytest.approx(40.6826) and s.body == "From $60"


def test_geo_sources_registry():
    assert set(ad.geo_sources()) == {"ticketmaster", "seatgeek"}


def test_ingest_endpoint_accepts_point(client, tm_live):
    r = client.post("/signals/ingest", json={"source": "ticketmaster", "lat": 51.5, "lng": -0.12, "radius_km": 20})
    assert r.status_code == 200 and r.json()["count"] == 2
    assert client.post("/signals/ingest", json={"source": "ticketmaster", "lat": 51.5}).status_code == 422


# ---------------------------------------------------------------------------
# /discover
# ---------------------------------------------------------------------------

def test_discover_scans_then_serves_from_cache(client, tm_live, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    monkeypatch.setattr(cfg.settings, "enable_mock_adapters", False)

    r = client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["center"]["city"] == "London" and d["center"]["country_code"] == "GB"
    cov = d["coverage"]
    assert cov["scanned"] is True and cov["reason"] == "never_scanned"
    assert cov["sources_run"] == ["ticketmaster"] and cov["sources_skipped"] == ["seatgeek"]
    assert cov["signals_created"] == 2 and cov["events_created"] == 2
    assert d["total"] == 2
    titles = {e["title"] for e in d["items"]}
    assert titles == {"Burna Boy Live", "Arsenal v Chelsea"}
    assert all(e["venue"]["city"] == "London" for e in d["items"])

    # Second call: area is fresh → no scan, same results
    d2 = client.get("/discover?lat=51.51&lng=-0.13&radius_km=20").json()
    assert d2["coverage"]["scanned"] is False and d2["coverage"]["reason"] == "fresh"
    assert d2["coverage"]["last_scanned_at"] is not None
    assert d2["total"] == 2

    # refresh=true forces a re-scan; signals dedupe by external_id so nothing doubles
    d3 = client.get("/discover?lat=51.51&lng=-0.13&radius_km=20&refresh=true").json()
    assert d3["coverage"]["scanned"] is True and d3["coverage"]["reason"] == "forced"
    assert d3["total"] == 2
    assert client.get("/events").json()["total"] == 2


def test_discover_category_and_query_filters(client, tm_live, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20")
    assert client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20&category=sports").json()["total"] == 1
    assert client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20&q=burna").json()["total"] == 1
    assert client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20&category=food").json()["total"] == 0


def test_discover_excludes_events_outside_radius(client, tm_live, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20")
    # Paris is 340km away — fresh scan there returns London events? No.
    d = client.get("/discover?lat=48.8566&lng=2.3522&radius_km=20&scan=false").json()
    assert d["total"] == 0 and d["coverage"]["reason"] == "never_scanned" and d["center"]["city"] == "Paris"


def test_discover_with_no_configured_sources(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "ticketmaster_api_key", None)
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    monkeypatch.setattr(cfg.settings, "enable_mock_adapters", False)
    d = client.get("/discover?lat=6.5244&lng=3.3792").json()
    assert d["coverage"]["scanned"] is False and d["coverage"]["reason"] == "no_sources"
    assert sorted(d["coverage"]["sources_skipped"]) == ["seatgeek", "ticketmaster"]
    assert d["radius_km"] == 25.0


def test_discover_stale_area_rescans(client, tm_live, monkeypatch, db_session):
    from app.core import config as cfg
    from app.models.event import CoverageCell
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=5")
    # age every cell past the staleness window
    for c in db_session.query(CoverageCell).all():
        c.last_scanned_at = datetime.now(timezone.utc) - timedelta(minutes=cfg.settings.discover_stale_minutes + 1)
    db_session.commit()
    d = client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=5").json()
    assert d["coverage"]["scanned"] is True and d["coverage"]["reason"] == "stale"


def test_force_scan_and_coverage_listing(client, tm_live, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    r = client.post("/discover/scan", json={"lat": 51.5074, "lng": -0.1278, "radius_km": 10})
    assert r.status_code == 200 and r.json()["scanned"] is True
    cells = client.get("/discover/coverage").json()
    assert cells and cells[0]["city"] == "London" and cells[0]["scan_count"] == 1
    assert client.get("/stats").json()["areas_covered"] == len(cells)


def test_one_broken_source_does_not_sink_the_scan(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "ticketmaster_api_key", "k")
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", "cid")

    def handler(request):
        if "ticketmaster" in str(request.url):
            return httpx.Response(500, text="upstream down")
        return httpx.Response(200, json={"events": [{"id": 1, "title": "Only SeatGeek", "type": "concert",
                                                     "datetime_utc": "2030-01-01T20:00:00",
                                                     "venue": {"name": "V", "city": "London", "location": {"lat": 51.5, "lon": -0.12}}}],
                                         "meta": {"total": 1, "per_page": 100}})

    monkeypatch.setattr(ad.BaseAdapter, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    d = client.get("/discover?lat=51.5&lng=-0.12&radius_km=10").json()
    assert d["coverage"]["scanned"] is True
    assert set(d["coverage"]["sources_run"]) == {"ticketmaster", "seatgeek"}
    assert any("500" in e for e in d["coverage"]["errors"])
    assert d["total"] == 1
    runs = {r["source"]: r["status"] for r in client.get("/signals/runs").json()["items"]}
    assert runs == {"ticketmaster": "failed", "seatgeek": "completed"}


# ---------------------------------------------------------------------------
# /geocode
# ---------------------------------------------------------------------------

def test_geocode_endpoints(client):
    r = client.get("/geocode?q=Lagos").json()
    assert r[0]["city"] == "Lagos" and r[0]["country_code"] == "NG"
    assert client.get("/geocode?q=zzzz-nowhere").json() == []
    assert client.get("/geocode/reverse?lat=-1.29&lng=36.82").json()["city"] == "Nairobi"
    assert client.get("/geocode/reverse?lat=0&lng=-140").status_code == 404


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def test_scheduler_loads_geo_and_coverage_jobs(monkeypatch):
    from app.core import config as cfg
    from app.workers.scheduler import _load_jobs
    monkeypatch.setattr(cfg.settings, "scheduler_sources",
        '[{"source":"ticketmaster","lat":6.52,"lng":3.38,"radius_km":50,"interval_minutes":180},'
        ' {"source":"coverage","interval_minutes":120}, {"bad": true}]')
    jobs = _load_jobs()
    assert [j.source for j in jobs] == ["ticketmaster", "coverage"]
    assert jobs[0].lat == 6.52 and jobs[0].radius_km == 50


def test_rescan_recent_cells_groups_by_city(client, tm_live, monkeypatch, db_session):
    from app.core import config as cfg
    from app.services.discovery import rescan_recent_cells
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=5")
    client.get("/discover?lat=51.52&lng=-0.10&radius_km=5")        # still London
    out = rescan_recent_cells(db_session)
    assert out["areas"] == 1 and out["status"] == "completed"


def test_ticketed_structured_listing_is_confirmed_on_its_own(client, tm_live, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    d = client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=20").json()
    burna = next(e for e in d["items"] if e["title"] == "Burna Boy Live")
    assert burna["structure_type"] == "structured"
    assert burna["confidence_score"] >= cfg.settings.confirmed_confidence_threshold


def test_wide_scan_covers_narrow_requests_but_not_vice_versa(client, tm_live, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "seatgeek_client_id", None)
    # 5 km scan first…
    client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=5")
    # …does not satisfy a 100 km request
    d = client.get("/discover?lat=51.5074&lng=-0.1278&radius_km=100").json()
    assert d["coverage"]["scanned"] is True and d["coverage"]["reason"] == "stale"
    # …but the 100 km scan now satisfies any smaller request inside it
    d = client.get("/discover?lat=51.52&lng=-0.10&radius_km=5").json()
    assert d["coverage"]["scanned"] is False and d["coverage"]["reason"] == "fresh"
    d = client.get("/discover?lat=51.6&lng=-0.3&radius_km=30").json()
    assert d["coverage"]["scanned"] is False and d["coverage"]["reason"] == "fresh"
    # a request poking out of the circle is only partially covered → rescan
    d = client.get("/discover?lat=52.2&lng=-0.1&radius_km=40").json()
    assert d["coverage"]["scanned"] is True and d["coverage"]["reason"] == "stale"
    # far away → never scanned
    d = client.get("/discover?lat=48.85&lng=2.35&radius_km=10&scan=false").json()
    assert d["coverage"]["reason"] == "never_scanned"
    # one row per distinct centre, with the largest fresh radius kept
    rows = client.get("/discover/coverage").json()
    london = next(r for r in rows if abs(r["latitude"] - 51.5074) < 1e-6)
    assert london["radius_km"] == 100
