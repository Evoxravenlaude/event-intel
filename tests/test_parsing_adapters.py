"""
Parsing + adapter tests. Network is replaced with httpx.MockTransport.
"""
from __future__ import annotations
from datetime import datetime, timezone

import httpx
import pytest

from app.services import adapters as ad
from app.services.parsing import (
    build_external_id,
    extract_jsonld_events,
    html_to_signal,
    infer_category,
    infer_times_from_text,
    parse_datetime,
    stable_id,
    truncate,
)


# ---------------------------------------------------------------------------
# Category inference — whole-word matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Tamasha la muziki Nairobi", "culture"),          # "ai" inside Nairobi used to force tech
    ("Party at the start of summer", "nightlife"),     # "art" inside start/party used to force culture
    ("Daoud's birthday", "general"),                   # "dao" inside Daoud
    ("New device launch", "general"),                  # "dev" inside device
    ("Lagos Web3 Builders Meetup", "web3"),
    ("AI hackathon for developers", "tech"),
    ("Google I/O extended Accra", "tech"),             # keyword with punctuation
    ("Fintech founders pitch night", "business"),
    ("Conférence sur l'intelligence artificielle à Dakar", "tech"),
    ("Mkutano wa biashara na wawekezaji", "business"),
    ("", None),
    (None, None),
])
def test_infer_category(text, expected):
    assert infer_category(text) == expected


def test_infer_category_prefers_more_distinct_hits():
    # 1 business hit ("conference") vs 3 web3 hits
    assert infer_category("Conference on blockchain, DeFi and NFTs") == "web3"


def test_live_words_and_relative_times():
    start, end = infer_times_from_text("Doors open tonight at Yaba")
    assert start is not None and end > start
    start, _ = infer_times_from_text("Demain soir à Dakar")
    assert start is not None
    # "today" must be a whole word — "todays" shouldn't match… but "today's" should
    assert infer_times_from_text("Todays specials") == (None, None)
    assert infer_times_from_text("Today's meetup") != (None, None)


def test_parse_datetime_handles_date_objects_and_naive():
    from datetime import date
    assert parse_datetime(date(2030, 5, 1)) == datetime(2030, 5, 1, tzinfo=timezone.utc)
    assert parse_datetime("2030-05-01T10:00:00") == datetime(2030, 5, 1, 10, tzinfo=timezone.utc)
    assert parse_datetime("2030-05-01T10:00:00+01:00") == datetime(2030, 5, 1, 9, tzinfo=timezone.utc)
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None


def test_truncate_respects_column_width():
    assert truncate("x" * 300, 255) is not None
    assert len(truncate("x" * 300, 255)) == 255
    assert truncate("short", 255) == "short"
    assert truncate(None, 255) is None


def test_stable_id_is_deterministic_and_distinct():
    assert stable_id("luma", "u", "A", None) == stable_id("luma", "u", "A", None)
    assert stable_id("luma", "u", "A", None) != stable_id("luma", "u", "B", None)
    assert build_external_id("https://lu.ma/cal.ics", "ignored") == "lu.ma/cal.ics"


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------

JSONLD_PAGE = """
<html><head><title>Fallback title</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event","name":"Lagos Web3 Summit 2030",
 "description":"<p>The biggest <b>web3</b> gathering in West Africa.</p>",
 "startDate":"2030-03-10T09:00:00+01:00","endDate":"2030-03-10T18:00:00+01:00",
 "url":"https://lu.ma/lagos-web3-2030",
 "location":{"@type":"Place","name":"Landmark Centre","address":{"@type":"PostalAddress","addressLocality":"Lagos","addressCountry":"NG"},
             "geo":{"@type":"GeoCoordinates","latitude":"6.4281","longitude":"3.4219"}},
 "organizer":{"@type":"Organization","name":"Web3 Lagos"}}
</script></head><body><p>Some body text</p></body></html>
"""


def test_extract_jsonld_events():
    events = extract_jsonld_events(JSONLD_PAGE)
    assert len(events) == 1 and events[0]["name"] == "Lagos Web3 Summit 2030"


def test_html_to_signal_prefers_jsonld():
    sig = html_to_signal("luma", "luma", "https://lu.ma/x", JSONLD_PAGE, city="Lagos")
    assert sig.title == "Lagos Web3 Summit 2030"
    assert sig.body.startswith("The biggest web3 gathering")   # HTML stripped
    assert "Organizer: Web3 Lagos" in sig.body
    assert sig.detected_start_time == datetime(2030, 3, 10, 8, tzinfo=timezone.utc)
    assert sig.location_text == "Landmark Centre"
    assert sig.latitude == pytest.approx(6.4281)
    assert sig.url == "https://lu.ma/lagos-web3-2030"
    assert sig.external_id == "lu.ma/lagos-web3-2030"
    assert sig.normalized_category == "web3"
    assert sig.source_confidence == 0.8


def test_html_to_signal_falls_back_to_text():
    html = "<html><head><title>Accra Tech Meetup tonight</title></head><body><p>Developers pull up.</p></body></html>"
    sig = html_to_signal("eventbrite", "eventbrite", "https://ex.com/e/1", html, city="Accra")
    assert sig.title == "Accra Tech Meetup tonight"
    assert sig.normalized_category == "tech"
    assert sig.detected_start_time is not None            # "tonight"
    assert sig.source_confidence == pytest.approx(0.67)   # live-word boost
    assert sig.location_text == "Accra"


# ---------------------------------------------------------------------------
# Adapters over a mock transport
# ---------------------------------------------------------------------------

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:evt-1@lu.ma
SUMMARY:Web3 Lagos Builders
DESCRIPTION:Onchain builders night
LOCATION:Landmark Centre\\, Lagos
DTSTART:20300301T180000Z
DTEND:20300301T210000Z
URL:https://lu.ma/evt-1
END:VEVENT
BEGIN:VEVENT
UID:evt-2@lu.ma
SUMMARY:Accra Fintech Founders
DTSTART;VALUE=DATE:20300315
DTEND;VALUE=DATE:20300316
END:VEVENT
BEGIN:VEVENT
SUMMARY:No UID event
DTSTART:20300401T100000Z
END:VEVENT
END:VCALENDAR
"""

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Lagos Tech</title>
<item><title>Yaba Dev Night tonight</title><link>https://t.me/lagostech/1</link><guid>tg-1</guid>
<description>&lt;b&gt;Developers&lt;/b&gt; pull up</description><pubDate>Mon, 01 Jan 2030 10:00:00 GMT</pubDate></item>
<item><title>Unrelated cooking post</title><link>https://t.me/lagostech/2</link><guid>tg-2</guid><description>Jollof</description></item>
</channel></rss>"""


@pytest.fixture()
def mock_http(monkeypatch):
    """Route every adapter HTTP call through a MockTransport keyed by URL substring."""
    routes: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for key, resp in routes.items():
            if key in str(request.url):
                return resp
        return httpx.Response(404, text="not mocked")

    def _client(self):
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)

    monkeypatch.setattr(ad.BaseAdapter, "_client", _client)
    return routes


def test_luma_ics_each_event_gets_its_own_external_id(mock_http):
    """All VEVENTs in one feed used to share the feed URL as external_id → one signal."""
    mock_http["cal.ics"] = httpx.Response(200, content=ICS, headers={"content-type": "text/calendar"})
    result = ad.LumaAdapter().fetch("Lagos", None, ["https://lu.ma/cal.ics"])
    assert result.fetched_count == 1
    ids = [s.external_id for s in result.items]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert ids[0] == "evt-1@lu.ma"
    first = result.items[0]
    assert first.url == "https://lu.ma/evt-1"
    assert first.location_text == "Landmark Centre, Lagos"
    assert first.detected_start_time == datetime(2030, 3, 1, 18, tzinfo=timezone.utc)
    assert first.normalized_category == "web3"
    # all-day event (DATE value) is accepted and normalised to midnight UTC
    assert result.items[1].detected_start_time == datetime(2030, 3, 15, tzinfo=timezone.utc)


def test_luma_page_uses_jsonld(mock_http):
    mock_http["lu.ma/page"] = httpx.Response(200, text=JSONLD_PAGE, headers={"content-type": "text/html"})
    result = ad.LumaAdapter().fetch("Lagos", None, ["https://lu.ma/page"])
    assert result.items[0].title == "Lagos Web3 Summit 2030"
    assert result.items[0].source_confidence == 0.86


def test_feed_adapter_fetches_with_httpx_and_filters_by_query(mock_http):
    mock_http["feed.xml"] = httpx.Response(200, content=RSS, headers={"content-type": "application/rss+xml"})
    result = ad.FeedAdapter("telegram", []).fetch("Lagos", "dev", ["https://t.me/feed.xml"])
    assert result.fetched_count == 1
    assert [s.title for s in result.items] == ["Yaba Dev Night tonight"]
    sig = result.items[0]
    assert sig.external_id == "tg-1"
    assert sig.body == "Developers pull up"             # HTML entities decoded + tags stripped
    assert sig.posted_at == datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    assert sig.detected_start_time is not None          # "tonight"


def test_feed_adapter_reports_http_errors_instead_of_mocking(mock_http):
    result = ad.FeedAdapter("telegram", []).fetch("Lagos", None, ["https://t.me/missing.xml"])
    assert result.items == []
    assert result.errors and "404" in result.errors[0]


def test_eventbrite_page_url_parses_jsonld(mock_http):
    mock_http["eventbrite.com/e/"] = httpx.Response(200, text=JSONLD_PAGE)
    result = ad.EventbriteAdapter().fetch("Lagos", None, ["https://www.eventbrite.com/e/123"])
    assert result.items[0].title == "Lagos Web3 Summit 2030"
    assert result.items[0].source_confidence == 0.85


def test_eventbrite_org_api(mock_http, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "eventbrite_private_token", "tok")
    monkeypatch.setattr(cfg.settings, "eventbrite_organization_id", "org1")
    mock_http["organizations/org1/events"] = httpx.Response(200, json={
        "events": [{
            "id": "987", "name": {"text": "Accra Startup Pitch Night"}, "description": {"text": "Founders pitch"},
            "url": "https://www.eventbrite.com/e/987",
            "start": {"utc": "2030-02-01T18:00:00Z"}, "end": {"utc": "2030-02-01T21:00:00Z"},
            "venue": {"name": "Impact Hub Accra", "latitude": "5.6037", "longitude": "-0.1870", "address": {"city": "Accra"}},
        }],
        "pagination": {"has_more_items": False},
    })
    result = ad.EventbriteAdapter().fetch("Accra", "pitch", None)
    assert result.fetched_count == 1
    sig = result.items[0]
    assert sig.external_id == "987"
    assert sig.location_text == "Impact Hub Accra"
    assert sig.latitude == pytest.approx(5.6037)
    assert sig.normalized_category == "business"


def test_x_api_v2_has_no_english_only_filter(mock_http, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "x_bearer_token", "tok")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params["query"]
        return httpx.Response(200, json={"data": [
            {"id": "1", "text": "Web3 thing dey happen for Yaba tonight\nPull up!", "created_at": "2030-01-01T10:00:00Z"},
        ]})

    monkeypatch.setattr(ad.BaseAdapter, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    result = ad.XAdapter().fetch("Lagos", "web3", None)
    assert "lang:en" not in seen["query"]
    assert "-is:retweet" in seen["query"]
    sig = result.items[0]
    assert sig.title == "Web3 thing dey happen for Yaba tonight"   # first line only
    assert sig.normalized_category == "web3"
    assert sig.url == "https://x.com/i/web/status/1"


def test_mock_items_have_stable_ids_and_mark_themselves(mock_http):
    r1 = ad.FeedAdapter("telegram", []).fetch("Lagos", "web3", None)
    r2 = ad.FeedAdapter("telegram", []).fetch("Lagos", "web3", None)
    assert r1.items[0].external_id == r2.items[0].external_id
    assert "Mock" in r1.items[0].body


def test_ingest_rejects_non_http_urls(client):
    r = client.post("/signals/ingest", json={"source": "luma", "urls": ["file:///etc/passwd"]})
    assert r.status_code == 422


def test_ingest_records_partial_failure(client, mock_http):
    mock_http["ok.ics"] = httpx.Response(200, content=ICS)
    r = client.post("/signals/ingest", json={"source": "luma", "city": "Lagos",
                                             "urls": ["https://lu.ma/ok.ics", "https://lu.ma/broken.ics"]})
    assert r.status_code == 200
    assert r.json()["count"] == 3
    run = client.get("/signals/runs").json()["items"][0]
    assert run["status"] == "partial"
    assert "broken.ics" in run["error"]


def test_long_titles_are_truncated_not_rejected(client):
    r = client.post("/signals", json={"source_type": "manual", "title": "x" * 300})
    assert r.status_code == 422  # API callers get a clear validation error…
    # …while adapters truncate before it ever reaches the schema
    assert len(truncate("x" * 300, 255)) == 255


def test_category_tie_breaks_on_specificity():
    assert infer_category("Street Food Festival") == "food"        # "street food" beats "festival"
    assert infer_category("Jazz Festival") == "culture"
