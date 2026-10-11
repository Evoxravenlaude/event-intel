"""
API behaviour added or corrected in the audit:
radius pagination, text search, /stats, /ready, PATCH, auth variants, CORS on 401.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone


def _mk(client, title, lat, lng, city="Lagos", **kw):
    payload = {"title": title, "venue": {"name": f"{title} venue", "city": city, "latitude": lat, "longitude": lng}}
    payload.update(kw)
    r = client.post("/events", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Radius search: totals and pages must be computed AFTER the distance filter
# ---------------------------------------------------------------------------

def test_radius_search_paginates_after_filtering(client):
    # 6 events inside 3km of Lagos Island, 4 far away (Abuja)
    for i in range(6):
        _mk(client, f"Near {i}", 6.45 + i * 0.001, 3.40 + i * 0.001)
    for i in range(4):
        _mk(client, f"Far {i}", 9.05 + i * 0.01, 7.49, city="Abuja")

    page1 = client.get("/events?lat=6.45&lng=3.40&radius_km=3&page=1&page_size=4").json()
    page2 = client.get("/events?lat=6.45&lng=3.40&radius_km=3&page=2&page_size=4").json()
    assert page1["total"] == 6 and page2["total"] == 6
    assert len(page1["items"]) == 4 and len(page2["items"]) == 2
    titles = {e["title"] for e in page1["items"] + page2["items"]}
    assert titles == {f"Near {i}" for i in range(6)}


def test_radius_requires_both_coordinates(client):
    assert client.get("/events?lat=6.45").status_code == 422


def test_radius_uses_default_radius_setting(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "default_radius_km", 1.0)
    _mk(client, "Close", 6.4500, 3.4000)
    _mk(client, "Five km", 6.4950, 3.4000)   # ≈5km north
    assert client.get("/events?lat=6.45&lng=3.40").json()["total"] == 1
    assert client.get("/events?lat=6.45&lng=3.40&radius_km=10").json()["total"] == 2


def test_events_without_coordinates_are_excluded_from_radius_search(client):
    client.post("/events", json={"title": "No venue"})
    client.post("/events", json={"title": "Venue no coords", "venue": {"name": "Somewhere", "city": "Lagos"}})
    _mk(client, "Has coords", 6.45, 3.40)
    assert client.get("/events?lat=6.45&lng=3.40&radius_km=5").json()["total"] == 1


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_city_filter_matches_venue_name_and_address_too(client):
    client.post("/events", json={"title": "A", "venue": {"name": "Landmark Centre, Lagos"}})
    client.post("/events", json={"title": "B", "venue": {"name": "iHub", "city": "Nairobi"}})
    client.post("/events", json={"title": "C", "venue": {"name": "Hall", "address": "12 Marina, Lagos Island"}})
    titles = {e["title"] for e in client.get("/events?city=lagos").json()["items"]}
    assert titles == {"A", "C"}


def test_text_search_and_multi_status(client):
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    client.post("/events", json={"title": "Afrobeats Night", "description": "music", "start_time": future})
    client.post("/events", json={"title": "Dev Meetup", "description": "afrobeats after-party"})
    r = client.get("/events?q=afrobeats").json()
    assert r["total"] == 2
    r = client.get("/events?status=upcoming,ongoing").json()
    assert [e["title"] for e in r["items"]] == ["Afrobeats Night"]
    r = client.get("/events?min_confidence=0.99").json()
    assert r["total"] == 0


def test_sort_by_confidence(client):
    client.post("/events", json={"title": "Low"})
    client.post("/events", json={"title": "High", "start_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                                 "venue": {"name": "V", "latitude": 1.0, "longitude": 1.0}})
    r = client.get("/events?sort=confidence").json()
    assert r["items"][0]["title"] == "High"


def test_signal_search_and_detail(client):
    client.post("/signals", json={"source_type": "manual", "title": "Yaba Dev Night", "body": "pull up"})
    sid = client.post("/signals", json={"source_type": "manual", "title": "Other", "body": "nothing"}).json()["id"]
    assert client.get("/signals?q=yaba").json()["total"] == 1
    assert client.get(f"/signals/{sid}").json()["title"] == "Other"
    assert client.get("/signals/9999").status_code == 404


def test_runs_pagination_and_filter(client):
    client.post("/signals/ingest", json={"source": "telegram", "city": "Lagos"})
    client.post("/signals/ingest", json={"source": "linkedin", "city": "Accra"})
    r = client.get("/signals/runs?source=telegram").json()
    assert r["total"] == 1 and r["items"][0]["source"] == "telegram"
    assert client.get("/signals/runs?page_size=1").json()["total"] == 2


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------

def test_patch_event_partial_update(client):
    ev = client.post("/events", json={"title": "Old", "category": "tech"}).json()
    r = client.patch(f"/events/{ev['id']}", json={"title": "New", "venue": {"name": "Landmark", "city": "Lagos"}})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "New" and body["category"] == "tech" and body["venue"]["city"] == "Lagos"
    r = client.patch(f"/events/{ev['id']}", json={"status": "ended"})
    assert r.json()["status"] == "ended"
    assert client.patch("/events/9999", json={"title": "x"}).status_code == 404
    assert client.patch(f"/events/{ev['id']}", json={"status": "bogus"}).status_code == 422


# ---------------------------------------------------------------------------
# /stats and /ready
# ---------------------------------------------------------------------------

def test_stats_reflects_data(client):
    _mk(client, "Lagos One", 6.45, 3.40)
    _mk(client, "Lagos Two", 6.46, 3.41)
    _mk(client, "Accra One", 5.60, -0.18, city="Accra", category="web3")
    client.post("/signals", json={"source_type": "telegram", "title": "s1"})
    client.post("/signals/ingest", json={"source": "luma", "city": "Lagos"})
    s = client.get("/stats").json()
    assert s["events_total"] == 3
    assert s["signals_total"] == 2
    assert s["signals_unprocessed"] == 2
    assert s["review_pending"] == 0
    assert {b["key"]: b["count"] for b in s["by_city"]} == {"Lagos": 2, "Accra": 1}
    assert {b["key"] for b in s["by_source"]} == {"telegram", "luma"}
    assert "luma" in s["sources_last_run"]


def test_ready_reports_database_and_schema(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"
    assert r.json()["embeddings"] == "disabled"


def test_health_includes_version(client):
    assert "version" in client.get("/health").json()


# ---------------------------------------------------------------------------
# Auth + CORS
# ---------------------------------------------------------------------------

def test_bearer_token_accepted(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    assert client.get("/events", headers={"Authorization": "Bearer k"}).status_code == 200
    assert client.get("/events", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_ready_and_root_exempt_from_auth(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    assert client.get("/ready").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/stats").status_code == 401


def test_cors_preflight_passes_without_key(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    r = client.options("/events", headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "x-api-key"})
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


def test_401_response_still_carries_cors_headers(client, monkeypatch):
    """CORS must wrap the auth middleware, otherwise browsers see an opaque error instead of a 401."""
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    r = client.get("/events", headers={"Origin": "https://app.example"})
    assert r.status_code == 401
    assert "access-control-allow-origin" in r.headers


def test_request_id_header(client):
    r = client.get("/events", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
    assert client.get("/events").headers.get("x-request-id")


def test_unique_violation_maps_to_409(client, db_session):
    """Direct DB-level duplicate (bypassing the upsert) → 409, not 500."""
    from app.models.event import Organizer
    db_session.add(Organizer(name="Dup Org")); db_session.commit()
    # Organizer names are unique; trigger via create_event's get_or_create path is an upsert,
    # so exercise the handler directly through the venue alias path instead:
    from sqlalchemy.exc import IntegrityError
    from app.main import integrity_error_handler
    import asyncio
    from fastapi import Request
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [], "query_string": b""}
    resp = asyncio.get_event_loop().run_until_complete(
        integrity_error_handler(Request(scope), IntegrityError("stmt", {}, Exception("dup")))
    )
    assert resp.status_code == 409


def test_robots_and_favicon_public_even_with_api_key(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "k")
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Allow: /$" in r.text and "Disallow: /signals" in r.text
    assert client.get("/favicon.ico").headers["content-type"].startswith("image/svg")
