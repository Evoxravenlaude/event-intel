"""
Regression tests for clustering + review behaviour.

Each of these pins a bug found in the audit:
- naive/aware datetime crash on SQLite
- queued signals re-queued on every pass
- seeded events losing the signal's coordinates
- 0.0 lat/lng treated as "no coordinates"
- webhook fired on every link instead of on the confirmed transition
- reject / discard / recluster semantics
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.event import Event, EventEvidence, RawSignal, ReviewQueueItem, Venue


def _future(days=2, hours=0):
    return datetime.now(timezone.utc) + timedelta(days=days, hours=hours)


def _post_signal(client, **kw):
    payload = {"source_type": "manual", "source_confidence": 0.8}
    payload.update(kw)
    r = client.post("/signals", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _cluster(client):
    r = client.post("/signals/cluster")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Timezone safety
# ---------------------------------------------------------------------------

def test_cluster_with_timed_signal_against_stored_event_does_not_crash(client):
    """Events read back from SQLite used to be naive → TypeError in scoring."""
    start = _future()
    client.post("/events", json={"title": "Lagos Web3 Builders Meetup", "category": "web3", "start_time": start.isoformat()})
    _post_signal(client, external_id="tz-1", title="Lagos Web3 Builders Meetup", normalized_category="web3",
                 detected_start_time=start.isoformat())
    out = _cluster(client)
    # Exact same title + category + time → auto-link
    assert len(out["linked_signal_ids"]) == 1
    assert out["created_event_ids"] == []


def test_naive_datetime_in_payload_is_treated_as_utc(client):
    r = client.post("/events", json={"title": "Naive time", "start_time": "2030-01-01T18:00:00"})
    assert r.status_code == 201
    assert r.json()["start_time"].endswith("Z") or "+00:00" in r.json()["start_time"]
    assert r.json()["status"] == "upcoming"


def test_datetimes_round_trip_as_aware_utc(db_session):
    from app.services.event_service import create_event
    from app.schemas.event import EventCreate
    ev = create_event(db_session, EventCreate(title="RT", start_time=datetime(2030, 1, 1, 12, tzinfo=timezone.utc)))
    db_session.expunge_all()
    loaded = db_session.execute(select(Event).where(Event.id == ev.id)).scalar_one()
    assert loaded.start_time.tzinfo is not None
    assert loaded.start_time == datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Review queue does not loop
# ---------------------------------------------------------------------------

def _make_uncertain_pair(client):
    """Seed an event and a signal that lands in the review band (0.35–0.55)."""
    start = _future()
    ev = client.post("/events", json={"title": "Accra Fintech Founders Night", "category": "business",
                                      "start_time": start.isoformat()}).json()
    # 2 of 4 title words shared → Jaccard 2/6 ≈ 0.33 + category 0.1 = 0.43 → review band
    _post_signal(client, external_id="rq-1", title="Fintech Founders Dinner Party", normalized_category="business")
    return ev


def test_queued_signal_is_not_requeued_on_next_pass(client):
    _make_uncertain_pair(client)
    first = _cluster(client)
    assert len(first["queued_review_ids"]) == 1

    second = _cluster(client)
    assert second["queued_review_ids"] == []
    assert second["created_event_ids"] == []
    assert client.get("/review-queue?status=pending").json()["total"] == 1


def test_review_item_includes_both_sides_of_the_match(client):
    ev = _make_uncertain_pair(client)
    _cluster(client)
    item = client.get("/review-queue").json()["items"][0]
    assert item["signal"]["title"] == "Fintech Founders Dinner Party"
    assert item["candidate_event"]["id"] == ev["id"]
    assert item["candidate_event"]["title"] == "Accra Fintech Founders Night"


def test_approve_link_attaches_evidence_and_marks_processed(client):
    ev = _make_uncertain_pair(client)
    item_id = _cluster(client)["queued_review_ids"][0]
    r = client.post(f"/review-queue/{item_id}/resolve", json={"action": "approve_link", "note": "same thing"})
    assert r.status_code == 200
    assert r.json() == {"id": item_id, "status": "approved", "event_id": ev["id"]}

    event = client.get(f"/events/{ev['id']}").json()
    assert event["evidence_count"] == 1
    sig = client.get("/signals?processed=true").json()
    assert sig["total"] == 1
    # Nothing left for the next pass
    assert _cluster(client) == {"created_event_ids": [], "linked_signal_ids": [], "queued_review_ids": [], "remaining_unprocessed": 0}


def test_reject_creates_a_separate_event_from_the_signal(client):
    ev = _make_uncertain_pair(client)
    item_id = _cluster(client)["queued_review_ids"][0]
    r = client.post(f"/review-queue/{item_id}/resolve", json={"action": "reject"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    assert body["event_id"] not in (None, ev["id"])
    new_event = client.get(f"/events/{body['event_id']}").json()
    assert new_event["title"] == "Fintech Founders Dinner Party"
    assert new_event["evidence_count"] == 1
    assert client.get("/events").json()["total"] == 2


def test_discard_links_nothing_and_creates_nothing(client):
    _make_uncertain_pair(client)
    item_id = _cluster(client)["queued_review_ids"][0]
    r = client.post(f"/review-queue/{item_id}/resolve", json={"action": "discard", "note": "spam"})
    assert r.status_code == 200
    assert r.json()["status"] == "discarded"
    assert client.get("/events").json()["total"] == 1
    assert client.get("/signals?processed=false").json()["total"] == 0
    assert _cluster(client)["queued_review_ids"] == []


def test_recluster_reevaluates_signal_after_event_was_fixed(client):
    ev = _make_uncertain_pair(client)
    item_id = _cluster(client)["queued_review_ids"][0]
    # Reviewer fixes the event title so the signal now matches strongly
    r = client.patch(f"/events/{ev['id']}", json={"title": "Fintech Founders Dinner Party"})
    assert r.status_code == 200
    r = client.post(f"/review-queue/{item_id}/resolve", json={"action": "recluster"})
    assert r.status_code == 200
    assert r.json()["status"] == "reclustered"
    assert r.json()["event_id"] == ev["id"]
    assert client.get(f"/events/{ev['id']}").json()["evidence_count"] == 1


def test_resolving_twice_is_rejected(client):
    _make_uncertain_pair(client)
    item_id = _cluster(client)["queued_review_ids"][0]
    assert client.post(f"/review-queue/{item_id}/resolve", json={"action": "discard"}).status_code == 200
    r = client.post(f"/review-queue/{item_id}/resolve", json={"action": "discard"})
    assert r.status_code == 400
    assert "already resolved" in r.json()["detail"]


def test_review_queue_default_lists_pending_only(client):
    _make_uncertain_pair(client)
    item_id = _cluster(client)["queued_review_ids"][0]
    client.post(f"/review-queue/{item_id}/resolve", json={"action": "discard"})
    assert client.get("/review-queue").json()["total"] == 0
    assert client.get("/review-queue?status=all").json()["total"] == 1
    assert client.get("/review-queue?status=discarded").json()["total"] == 1


# ---------------------------------------------------------------------------
# Geo
# ---------------------------------------------------------------------------

def test_seeded_event_inherits_signal_coordinates(client):
    _post_signal(client, external_id="geo-1", title="Yaba Dev Night", location_text="Yaba", latitude=6.5095, longitude=3.3711)
    out = _cluster(client)
    ev = client.get(f"/events/{out['created_event_ids'][0]}").json()
    assert ev["venue"]["latitude"] == pytest.approx(6.5095)
    assert ev["venue"]["longitude"] == pytest.approx(3.3711)
    # …and is therefore reachable via radius search
    hits = client.get("/events?lat=6.5244&lng=3.3792&radius_km=5").json()
    assert hits["total"] == 1


def test_zero_coordinates_are_valid(db_session):
    from app.services.clustering import _geo_similarity, DISTANCE_BOOST
    venue = Venue(name="Null Island Hall", latitude=0.0, longitude=0.0)
    db_session.add(venue); db_session.flush()
    event = Event(title="x", venue_id=venue.id); event.venue = venue
    sig = RawSignal(source_type="manual", latitude=0.0, longitude=0.0)
    assert _geo_similarity(sig, event) == DISTANCE_BOOST


# ---------------------------------------------------------------------------
# Thresholds + batch limit
# ---------------------------------------------------------------------------

def test_cluster_respects_limit_and_reports_remaining(client):
    titles = ["Alpha quantum gathering", "Beta jazz evening", "Gamma chess open", "Delta yoga sunrise", "Epsilon pottery class"]
    for i, t in enumerate(titles):
        _post_signal(client, external_id=f"lim-{i}", title=t)
    out = client.post("/signals/cluster?limit=2").json()
    assert len(out["created_event_ids"]) == 2
    assert out["remaining_unprocessed"] == 3


def test_thresholds_are_configurable(client, monkeypatch):
    from app.core import config as cfg
    # Make everything auto-link by lowering the link threshold below the review band
    monkeypatch.setattr(cfg.settings, "cluster_link_threshold", 0.4)
    monkeypatch.setattr(cfg.settings, "cluster_review_threshold", 0.3)
    _make_uncertain_pair(client)
    out = _cluster(client)
    assert len(out["linked_signal_ids"]) == 1
    assert out["queued_review_ids"] == []


def test_invalid_threshold_order_rejected():
    from app.core.config import Settings
    with pytest.raises(ValueError):
        Settings(cluster_link_threshold=0.3, cluster_review_threshold=0.5)


# ---------------------------------------------------------------------------
# Webhooks: fire on transition only
# ---------------------------------------------------------------------------

def test_webhook_fires_only_when_crossing_confirmed_threshold(monkeypatch):
    from app.services import webhooks
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "webhook_urls", "https://example.com/hook")
    monkeypatch.setattr(cfg.settings, "confirmed_confidence_threshold", 0.75)
    fired = []
    monkeypatch.setattr(webhooks, "fire_event_confirmed", lambda **kw: fired.append(kw) or 1)

    ev = Event(id=1, title="t", status="upcoming", category="tech")
    assert webhooks.fire_if_newly_confirmed(ev, 0.6, 0.7) is False     # still below
    assert webhooks.fire_if_newly_confirmed(ev, 0.6, 0.8) is True      # crossed
    assert webhooks.fire_if_newly_confirmed(ev, 0.8, 0.9) is False     # already confirmed
    assert len(fired) == 1
    assert fired[0]["event_id"] == 1


def test_webhook_payload_is_signed_when_secret_set(monkeypatch):
    import hashlib, hmac, json
    from app.services import webhooks
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "webhook_urls", "https://example.com/hook")
    monkeypatch.setattr(cfg.settings, "webhook_secret", "s3cr3t")
    captured = {}

    def fake_thread(target, args, daemon):
        captured["url"], captured["body"], captured["headers"] = args
        class T:
            def start(self): pass
        return T()

    monkeypatch.setattr(webhooks.threading, "Thread", fake_thread)
    n = webhooks.fire_event_confirmed(event_id=7, title="Signed", status="upcoming", confidence_score=0.9,
                                      category="web3", start_time=None)
    assert n == 1
    expected = hmac.new(b"s3cr3t", captured["body"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-Signature"] == f"sha256={expected}"
    assert json.loads(captured["body"])["event_id"] == 7


# ---------------------------------------------------------------------------
# Delete returns signals to the pool
# ---------------------------------------------------------------------------

def test_delete_event_unprocesses_its_signals(client):
    _post_signal(client, external_id="del-1", title="Delete me event")
    ev_id = _cluster(client)["created_event_ids"][0]
    assert client.delete(f"/events/{ev_id}").status_code == 204
    assert client.get(f"/events/{ev_id}").status_code == 404
    assert client.get("/signals?processed=false").json()["total"] == 1
    # Re-clustering recreates it cleanly
    assert len(_cluster(client)["created_event_ids"]) == 1


def test_colocated_same_category_events_are_not_queued(client):
    """Two different concerts at neighbouring venues must not land in review on location alone."""
    start_a = _future(1).isoformat(); start_b = _future(5).isoformat()
    _post_signal(client, external_id="col-1", title="Afrobeats Night Live", normalized_category="culture",
                 location_text="Venue 1", latitude=-1.292, longitude=36.822, detected_start_time=start_a)
    _post_signal(client, external_id="col-2", title="Jazz at the Terrace", normalized_category="culture",
                 location_text="Venue 2", latitude=-1.293, longitude=36.823, detected_start_time=start_b)
    out = _cluster(client)
    assert len(out["created_event_ids"]) == 2 and out["queued_review_ids"] == []


def test_same_night_same_venue_similar_title_still_links(client):
    start = _future(1).isoformat()
    _post_signal(client, external_id="lnk-1", title="Burna Boy Live in Concert", normalized_category="culture",
                 location_text="Arena", latitude=-1.292, longitude=36.822, detected_start_time=start)
    _post_signal(client, external_id="lnk-2", title="Burna Boy Concert", normalized_category="culture",
                 latitude=-1.292, longitude=36.822, detected_start_time=start)
    out = _cluster(client)
    assert len(out["created_event_ids"]) == 1 and len(out["linked_signal_ids"]) == 1


def test_identical_titles_in_different_cities_are_different_events(client):
    start = _future(2).isoformat()
    _post_signal(client, external_id="far-1", title="Jazz at the Terrace", normalized_category="culture",
                 location_text="Terrace Nairobi", latitude=-1.2921, longitude=36.8219, detected_start_time=start)
    _post_signal(client, external_id="far-2", title="Jazz at the Terrace", normalized_category="culture",
                 location_text="Terrace Kigali", latitude=-1.9441, longitude=30.0619, detected_start_time=start)
    out = _cluster(client)
    assert len(out["created_event_ids"]) == 2 and out["linked_signal_ids"] == [] and out["queued_review_ids"] == []


def test_identical_titles_weeks_apart_are_different_events(client):
    _post_signal(client, external_id="wk-1", title="Friday Jazz Night", normalized_category="culture",
                 latitude=-1.2921, longitude=36.8219, detected_start_time=_future(1).isoformat())
    _post_signal(client, external_id="wk-2", title="Friday Jazz Night", normalized_category="culture",
                 latitude=-1.2921, longitude=36.8219, detected_start_time=_future(8).isoformat())
    out = _cluster(client)
    assert len(out["created_event_ids"]) == 2 and out["linked_signal_ids"] == []


def test_untimed_ungeolocated_duplicate_still_links(client):
    """Vetoes need data on both sides — a bare repost of the same title still merges."""
    _post_signal(client, external_id="bare-1", title="Lagos Web3 Builders Meetup", normalized_category="web3")
    _post_signal(client, external_id="bare-2", title="Lagos Web3 Builders Meetup", normalized_category="web3")
    out = _cluster(client)
    assert len(out["created_event_ids"]) == 1 and len(out["linked_signal_ids"]) == 1


def test_same_venue_name_in_different_cities_is_not_merged(client):
    a = client.post("/events", json={"title": "A", "venue": {"name": "Impact Hub", "latitude": -1.2921, "longitude": 36.8219}}).json()
    b = client.post("/events", json={"title": "B", "venue": {"name": "Impact Hub", "latitude": -1.9441, "longitude": 30.0619}}).json()
    assert a["venue"]["id"] != b["venue"]["id"]
    assert a["venue"]["city"] == "Nairobi" and b["venue"]["city"] == "Kigali"   # city resolved from the point
    # …but the same venue reported twice from nearby points IS merged
    c = client.post("/events", json={"title": "C", "venue": {"name": "Impact Hub", "latitude": -1.2930, "longitude": 36.8225}}).json()
    assert c["venue"]["id"] == a["venue"]["id"]
