"""
Luma city feeds as a keyless geo source. Fixture ICS mirrors the real
api.lu.ma/ics/get?entity=discover feed captured on 2026-10-10 (Lagos).
"""
from __future__ import annotations
from datetime import datetime, timezone

import httpx
import pytest

from app.services import adapters as ad
from app.services import luma_places
from app.services.event_service import split_venue_text

LAGOS_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Luma//EN
BEGIN:VEVENT
UID:evt-oIdHlGRtUrqdAWW@events.lu.ma
DTSTAMP:20261010T220000Z
SUMMARY:Disc Club
DTSTART:20301011T160000Z
DTEND:20301011T220000Z
LOCATION:Il\xc3\xa9 MIMU, 1 Adeola Odeku St, Victoria Island, Lagos 106104, Lagos, Nigeria
DESCRIPTION:Get up-to-date information at: https://luma.com/47ve9zu1
GEO:6.4318;3.4210
END:VEVENT
BEGIN:VEVENT
UID:evt-iBEfoOBhYKK2uHi@events.lu.ma
DTSTAMP:20261010T220000Z
SUMMARY:CXCon'26
DTSTART:20301016T090000Z
LOCATION:https://luma.com/event/evt-iBEfoOBhYKK2uHi
END:VEVENT
BEGIN:VEVENT
UID:evt-cZMlGTGuMZkUsLG@events.lu.ma
DTSTAMP:20261010T220000Z
SUMMARY:2026 Incubation Hub Pitch Competition
DTSTART:20301015T090000Z
LOCATION:The Zone, Plot 9 Gbagada Industrial Scheme, Gbagada Expressway 105102, Lagos, Nigeria
GEO:6.5550;3.3890
END:VEVENT
BEGIN:VEVENT
UID:evt-far@events.lu.ma
DTSTAMP:20261010T220000Z
SUMMARY:Epe Lagoon Retreat
DTSTART:20301020T090000Z
LOCATION:Epe Resort, Epe, Lagos, Nigeria
GEO:6.5833;3.9833
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture()
def luma_http(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.params.get("id") == "discplace-ARF3ZNcu47bs56x":
            return httpx.Response(200, content=LAGOS_ICS, headers={"content-type": "text/calendar"})
        return httpx.Response(200, content=b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", headers={"content-type": "text/calendar"})

    monkeypatch.setattr(ad.BaseAdapter, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    return calls


def test_places_near_finds_lagos_not_nairobi():
    near = luma_places.places_near(6.5244, 3.3792, 25)
    assert [p.slug for p, _ in near] == ["lagos"]
    assert near[0][0].ics_url == "https://api.lu.ma/ics/get?entity=discover&id=discplace-ARF3ZNcu47bs56x"
    assert luma_places.places_near(0.4162, 9.4673, 25) == []          # Libreville: no Luma city
    europe = [p.slug for p, _ in luma_places.places_near(50.85, 4.35, 200, max_places=5)]
    assert europe[0] == "brussels" and "amsterdam" in europe


def test_live_directory_merges_over_snapshot(monkeypatch):
    payload = {"places": [
        {"place": {"slug": "kigali", "name": "Kigali", "api_id": "discplace-TEST",
                   "coordinate": {"latitude": -1.9441, "longitude": 30.0619}}, "event_count": 4},
        {"place": {"slug": "broken"}},
    ]}
    monkeypatch.setattr(luma_places, "_fetch_live", lambda: luma_places._parse_bootstrap(payload))
    luma_places.reset_cache()
    places = luma_places.all_places()
    assert any(p.slug == "kigali" for p in places)
    assert any(p.slug == "lagos" for p in places)            # snapshot kept
    assert not any(p.slug == "broken" for p in places)


def test_luma_geo_fetch_parses_real_feed_shape(luma_http):
    r = ad.LumaAdapter().fetch(None, None, None, geo=ad.GeoQuery(lat=6.5244, lng=3.3792, radius_km=25))
    titles = [s.title for s in r.items]
    # Epe (~67 km away) is in the Lagos feed but outside the 25 km circle
    assert titles == ["Disc Club", "CXCon'26", "2026 Incubation Hub Pitch Competition"]
    assert r.fetched_count == 4 and r.errors == []
    disc, cx, pitch = r.items
    assert disc.latitude == pytest.approx(6.4318) and disc.url == "https://luma.com/47ve9zu1"
    assert disc.detected_start_time == datetime(2030, 10, 11, 16, tzinfo=timezone.utc)
    # URL-only LOCATION = hidden address → not a venue; falls back to the city, keeps the link
    assert cx.location_text == "Lagos" and cx.url == "https://luma.com/event/evt-iBEfoOBhYKK2uHi"
    assert disc.external_id == "evt-oIdHlGRtUrqdAWW@events.lu.ma"
    assert len(luma_http) == 1


def test_luma_geo_returns_nothing_where_luma_has_no_city(luma_http):
    r = ad.LumaAdapter().fetch(None, None, None, geo=ad.GeoQuery(lat=0.4162, lng=9.4673, radius_km=25))
    assert r.items == [] and r.errors == [] and luma_http == []


def test_split_venue_text():
    assert split_venue_text("Ilé MIMU, 1 Adeola Odeku St, Victoria Island, Lagos") == ("Ilé MIMU", "Ilé MIMU, 1 Adeola Odeku St, Victoria Island, Lagos")
    assert split_venue_text("Lagos") == ("Lagos", None)


def test_discover_lagos_via_luma_end_to_end(client, luma_http, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "discover_sources", "luma")
    d = client.get("/discover?lat=6.5244&lng=3.3792&radius_km=25").json()
    cov = d["coverage"]
    assert cov["sources_run"] == ["luma"] and cov["errors"] == []
    assert cov["signals_created"] == 3 and cov["events_created"] == 3
    assert d["center"]["city"] == "Lagos"
    by_title = {e["title"]: e for e in d["items"]}
    assert set(by_title) == {"Disc Club", "CXCon'26", "2026 Incubation Hub Pitch Competition"}
    disc = by_title["Disc Club"]["venue"]
    assert disc["name"] == "Ilé MIMU" and disc["city"] == "Lagos" and disc["country_code"] == "NG"
    assert "Adeola Odeku" in disc["address"]
    assert by_title["Disc Club"]["structure_type"] == "structured"


def test_luma_is_a_configured_geo_source():
    assert ad.LumaAdapter().supports_geo and ad.LumaAdapter().is_configured()
    assert "luma" in ad.geo_sources()
