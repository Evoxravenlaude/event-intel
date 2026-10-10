"""
Source adapters: turn an external source into a list of RawSignalCreate.

Every adapter follows the same contract:

    fetch(city, query, urls) -> SourceAdapterResult(items, fetched_count)

- `urls` from the request override configured env URLs.
- Network I/O always goes through `_client()` so the configured timeout,
  redirects and a sane User-Agent apply everywhere (feedparser's own
  fetcher had no timeout and ignored proxies).
- When a source yields nothing *and* ENABLE_MOCK_ADAPTERS is true, a single
  clearly-labelled mock item is returned so the pipeline can be exercised
  without credentials. Mock items carry low confidence and a fixed
  external_id so re-ingesting updates rather than duplicates them.
"""
from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field

import feedparser
import httpx
from app.core.config import settings
from app.schemas.event import RawSignalCreate
from app.services.parsing import (
    MAX_EXTERNAL_ID,
    MAX_LOCATION,
    MAX_TITLE,
    build_external_id,
    extract_jsonld_events,
    html_to_signal,
    infer_category,
    infer_times_from_text,
    jsonld_event_to_signal,
    parse_datetime,
    stable_id,
    strip_html,
    truncate,
)

logger = logging.getLogger(__name__)

USER_AGENT = "event-intel/1.0 (+https://github.com/event-intel)"


@dataclass
class SourceAdapterResult:
    items: list[RawSignalCreate]
    fetched_count: int
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeoQuery:
    """A 'what is around this point' request. Adapters with supports_geo use it."""
    lat: float
    lng: float
    radius_km: float = 25.0


class BaseAdapter:
    source_name: str
    # True when the adapter can answer a GeoQuery directly (lat/lng/radius) —
    # these are the sources /discover runs for an arbitrary location on Earth.
    supports_geo: bool = False

    def fetch(
        self,
        city: str | None,
        query: str | None,
        urls: list[str] | None = None,
        geo: GeoQuery | None = None,
    ) -> SourceAdapterResult:
        raise NotImplementedError

    def is_configured(self) -> bool:
        """Whether live (non-mock) fetching is possible with current settings."""
        return True

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=settings.source_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml,text/calendar,*/*"},
        )

    def _mock(self, city: str | None, query: str | None, body: str, confidence: float = 0.5) -> RawSignalCreate:
        return RawSignalCreate(
            source_type=self.source_name,
            source_name=self.source_name,
            external_id=f"mock-{self.source_name}-{(city or 'global').lower()}-{(query or 'any').lower()}"[:MAX_EXTERNAL_ID],
            title=truncate(f"{query or self.source_name.title()} event in {city or 'Global'}", MAX_TITLE),
            body=body,
            location_text=truncate(city, MAX_LOCATION),
            source_confidence=confidence,
            normalized_category=infer_category(query or self.source_name),
        )


# ---------------------------------------------------------------------------
# Luma
# ---------------------------------------------------------------------------

class LumaAdapter(BaseAdapter):
    """
    Luma: ICS calendar feeds (every Luma calendar exposes one) or event /
    calendar page URLs (parsed via the schema.org JSON-LD Luma embeds).
    """
    source_name = "luma"

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        feed_urls = urls or settings.split_csv(settings.luma_feed_urls)
        items: list[RawSignalCreate] = []
        errors: list[str] = []
        fetched_count = 0
        with self._client() as client:
            for url in feed_urls:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    errors.append(f"{url}: {exc}")
                    logger.warning("Luma fetch failed for %s: %s", url, exc)
                    continue
                fetched_count += 1
                ctype = response.headers.get("content-type", "")
                if url.endswith(".ics") or "text/calendar" in ctype or response.text.lstrip().startswith("BEGIN:VCALENDAR"):
                    items.extend(self._parse_ics(url, response.content, city))
                else:
                    items.extend(self._parse_page(url, response.text, city))

        if not items and not errors and settings.enable_mock_adapters:
            items.append(self._mock(city, query, "Mock Luma result. Set LUMA_FEED_URLS to a Luma .ics feed or pass page URLs.", 0.55))
            fetched_count = max(fetched_count, 1)
        return SourceAdapterResult(items=items, fetched_count=fetched_count, errors=errors)

    def _parse_page(self, url: str, html: str, city: str | None) -> list[RawSignalCreate]:
        events = extract_jsonld_events(html)
        if not events:
            return [html_to_signal("luma", "luma", url, html, city)]
        out = []
        for data in events:
            sig = jsonld_event_to_signal(data, source="luma", source_name="luma", url=url, city=city, confidence=0.86)
            if sig:
                out.append(sig)
        return out

    def _parse_ics(self, url: str, content: bytes, city: str | None) -> list[RawSignalCreate]:
        from icalendar import Calendar
        try:
            calendar = Calendar.from_ical(content)
        except Exception as exc:  # icalendar raises bare ValueError subclasses
            logger.warning("Luma ICS parse failed for %s: %s", url, exc)
            return []
        items: list[RawSignalCreate] = []
        for component in calendar.walk():
            if component.name != "VEVENT":
                continue
            title = str(component.get("summary", "") or "Untitled event")
            description = str(component.get("description", "") or "")
            location_text = str(component.get("location", "") or "") or None
            event_url = str(component.get("url", "") or "") or None
            uid = str(component.get("uid", "") or "") or None

            start = component.get("dtstart")
            end = component.get("dtend")
            # .dt is a datetime for timed events but a *date* for all-day events;
            # parse_datetime normalises both to aware UTC.
            start_dt = parse_datetime(start.dt) if start is not None else None
            end_dt = parse_datetime(end.dt) if end is not None else None

            lat = lng = None
            geo = component.get("geo")
            if geo is not None and hasattr(geo, "latitude"):
                try:
                    lat, lng = float(geo.latitude), float(geo.longitude)
                except (TypeError, ValueError):
                    lat = lng = None

            items.append(
                RawSignalCreate(
                    source_type="luma",
                    source_name="luma",
                    # Each VEVENT must get its own id — the feed URL alone made every
                    # event in the calendar collapse into one signal.
                    external_id=truncate(uid, MAX_EXTERNAL_ID) or stable_id("luma", url, title, start_dt),
                    title=truncate(title, MAX_TITLE),
                    body=description or None,
                    location_text=truncate(location_text or city, MAX_LOCATION),
                    url=event_url or url,
                    detected_start_time=start_dt,
                    detected_end_time=end_dt,
                    latitude=lat,
                    longitude=lng,
                    source_confidence=0.86,
                    normalized_category=infer_category(f"{title} {description}"),
                )
            )
        return items


# ---------------------------------------------------------------------------
# Eventbrite
# ---------------------------------------------------------------------------

class EventbriteAdapter(BaseAdapter):
    """
    Eventbrite.

    The public /v3/events/search/ endpoint this adapter originally called was
    retired by Eventbrite in Feb 2020 and returns 404 for every token. The
    API now only lists events you own, so:

    1. EVENTBRITE_PRIVATE_TOKEN + EVENTBRITE_ORGANIZATION_ID →
       GET /v3/organizations/{id}/events/ (live + upcoming, paginated).
    2. Event / organizer page URLs → schema.org JSON-LD embedded in the page
       (works for any public Eventbrite event, no token needed).
    3. Mock item when nothing else yields results and mocks are enabled.
    """
    source_name = "eventbrite"
    API = "https://www.eventbriteapi.com/v3"

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        items: list[RawSignalCreate] = []
        errors: list[str] = []
        fetched_count = 0

        if settings.eventbrite_private_token and settings.eventbrite_organization_id:
            try:
                api_items, api_count = self._fetch_org_events(city, query)
                items.extend(api_items)
                fetched_count += api_count
            except httpx.HTTPError as exc:
                errors.append(f"eventbrite api: {exc}")
                logger.warning("Eventbrite API fetch failed: %s", exc)

        with self._client() as client:
            for url in urls or []:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    errors.append(f"{url}: {exc}")
                    logger.warning("Eventbrite page fetch failed for %s: %s", url, exc)
                    continue
                fetched_count += 1
                found = extract_jsonld_events(response.text)
                if found:
                    for data in found:
                        sig = jsonld_event_to_signal(
                            data, source="eventbrite", source_name="eventbrite", url=url, city=city, confidence=0.85,
                        )
                        if sig:
                            items.append(sig)
                else:
                    items.append(html_to_signal("eventbrite", "eventbrite", url, response.text, city))

        if not items and not errors and settings.enable_mock_adapters:
            items.append(self._mock(
                city, query,
                "Mock Eventbrite result. Set EVENTBRITE_PRIVATE_TOKEN + EVENTBRITE_ORGANIZATION_ID, "
                "or pass public Eventbrite event page URLs.",
                0.58,
            ))
            fetched_count = max(fetched_count, 1)
        return SourceAdapterResult(items=items, fetched_count=fetched_count, errors=errors)

    def _fetch_org_events(self, city: str | None, query: str | None) -> tuple[list[RawSignalCreate], int]:
        headers = {"Authorization": f"Bearer {settings.eventbrite_private_token}"}
        params = {"status": "live,started", "order_by": "start_asc", "expand": "venue"}
        items: list[RawSignalCreate] = []
        fetched = 0
        q = (query or "").lower()
        url = f"{self.API}/organizations/{settings.eventbrite_organization_id}/events/"
        with self._client() as client:
            for _ in range(10):  # hard cap on pagination
                response = client.get(url, headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
                for raw in payload.get("events", []):
                    fetched += 1
                    name = (raw.get("name") or {}).get("text") or ""
                    desc = (raw.get("description") or {}).get("text") or ""
                    if q and q not in f"{name} {desc}".lower():
                        continue
                    venue = raw.get("venue") or {}
                    addr = venue.get("address") or {}
                    loc_text = venue.get("name") or addr.get("city") or city
                    try:
                        lat = float(venue["latitude"]) if venue.get("latitude") else None
                        lng = float(venue["longitude"]) if venue.get("longitude") else None
                    except (TypeError, ValueError):
                        lat = lng = None
                    items.append(
                        RawSignalCreate(
                            source_type="eventbrite",
                            source_name="eventbrite",
                            external_id=str(raw.get("id")),
                            title=truncate(name or "Untitled event", MAX_TITLE),
                            body=desc or None,
                            location_text=truncate(loc_text, MAX_LOCATION),
                            url=raw.get("url"),
                            detected_start_time=parse_datetime((raw.get("start") or {}).get("utc")),
                            detected_end_time=parse_datetime((raw.get("end") or {}).get("utc")),
                            latitude=lat,
                            longitude=lng,
                            source_confidence=0.9,
                            normalized_category=infer_category(f"{name} {desc}"),
                        )
                    )
                pagination = payload.get("pagination") or {}
                if not pagination.get("has_more_items"):
                    break
                params["continuation"] = pagination.get("continuation")
        return items, fetched


# ---------------------------------------------------------------------------
# Generic RSS / Atom
# ---------------------------------------------------------------------------

class FeedAdapter(BaseAdapter):
    """Generic RSS/Atom feed adapter — used by Telegram, LinkedIn feeds, and X fallback."""

    def __init__(self, source_name: str, configured_urls: list[str]):
        self.source_name = source_name
        self.configured_urls = configured_urls

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        target_urls = urls or self.configured_urls
        items: list[RawSignalCreate] = []
        errors: list[str] = []
        fetched_count = 0
        q = (query or "").lower()
        with self._client() as client:
            for url in target_urls:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    errors.append(f"{url}: {exc}")
                    logger.warning("Feed fetch failed for %s: %s", url, exc)
                    continue
                fetched_count += 1
                feed = feedparser.parse(response.content)
                if feed.bozo and not feed.entries:
                    errors.append(f"{url}: not a parseable feed ({getattr(feed, 'bozo_exception', '')})")
                    continue
                for entry in feed.entries:
                    title = entry.get("title")
                    body = strip_html(entry.get("summary") or entry.get("description") or "")
                    full_text = f"{title or ''} {body or ''}"
                    if q and q not in full_text.lower():
                        continue
                    start_time, end_time = infer_times_from_text(full_text)
                    items.append(
                        RawSignalCreate(
                            source_type=self.source_name,
                            source_name=self.source_name,
                            external_id=truncate(entry.get("id"), MAX_EXTERNAL_ID)
                            or build_external_id(entry.get("link"), title)
                            or stable_id(self.source_name, url, title),
                            title=truncate(title, MAX_TITLE),
                            body=body,
                            location_text=truncate(city, MAX_LOCATION),
                            url=entry.get("link"),
                            posted_at=parse_datetime(entry.get("published") or entry.get("updated")),
                            detected_start_time=start_time,
                            detected_end_time=end_time,
                            source_confidence=0.63,
                            normalized_category=infer_category(full_text),
                        )
                    )
        if not items and not errors and settings.enable_mock_adapters:
            items.append(self._mock(city, query, f"Mock {self.source_name} result. Add feed URLs in env or payload urls."))
            fetched_count = max(fetched_count, 1)
        return SourceAdapterResult(items=items, fetched_count=fetched_count, errors=errors)


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------

class LinkedInAdapter(BaseAdapter):
    """
    LinkedIn adapter.

    LinkedIn has no public event API. This adapter handles two realistic sources:

    1. **RSS/Atom feed URLs** — company page update feeds or third-party scrapers.
       Configured via LINKEDIN_SOURCE_URLS (comma-separated).
    2. **Direct event page URLs** in the ingest payload — fetches the HTML and
       extracts structured event data from LinkedIn's JSON-LD embedding.
    """

    source_name = "linkedin"
    _EVENT_PAGE = re.compile(r"^https://(www\.)?linkedin\.com/events/", re.IGNORECASE)

    def __init__(self, configured_urls: list[str]):
        self.configured_urls = configured_urls

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        items: list[RawSignalCreate] = []
        errors: list[str] = []
        fetched_count = 0

        target_urls = urls or self.configured_urls
        feed_urls = [u for u in target_urls if not self._EVENT_PAGE.match(u)]
        event_page_urls = [u for u in target_urls if self._EVENT_PAGE.match(u)]

        if feed_urls:
            feed_result = FeedAdapter("linkedin", feed_urls).fetch(city, query, None)
            items.extend(feed_result.items)
            errors.extend(feed_result.errors)
            fetched_count += feed_result.fetched_count

        with self._client() as client:
            for url in event_page_urls:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    errors.append(f"{url}: {exc}")
                    continue
                fetched_count += 1
                sig = None
                for data in extract_jsonld_events(response.text):
                    sig = jsonld_event_to_signal(data, source="linkedin", source_name="linkedin", url=url, city=city, confidence=0.78)
                    if sig:
                        break
                items.append(sig or html_to_signal("linkedin", "linkedin", url, response.text, city))

        if not items and not errors and settings.enable_mock_adapters:
            items.append(self._mock(
                city, query,
                "Mock LinkedIn result. Provide LINKEDIN_SOURCE_URLS (RSS/Atom) or pass LinkedIn event page URLs in the ingest payload.",
            ))
            fetched_count = max(fetched_count, 1)
        return SourceAdapterResult(items=items, fetched_count=fetched_count, errors=errors)


# ---------------------------------------------------------------------------
# X (Twitter)
# ---------------------------------------------------------------------------

class XAdapter(BaseAdapter):
    """
    X (Twitter) adapter.

    Priority order:
    1. X API v2 recent-search endpoint when X_BEARER_TOKEN is configured.
    2. Feed URLs from env / payload (RSS/Atom bridges).
    3. Mock item when ENABLE_MOCK_ADAPTERS is true and nothing else yielded results.
    """

    source_name = "x"

    def __init__(self, configured_urls: list[str] | None = None):
        self.configured_urls = configured_urls or []

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        if settings.x_bearer_token and query and not urls:
            try:
                return self._fetch_api_v2(city, query)
            except httpx.HTTPError as exc:
                logger.warning("X API fetch failed: %s", exc)
                return SourceAdapterResult(items=[], fetched_count=0, errors=[f"x api: {exc}"])
        return FeedAdapter("x", self.configured_urls).fetch(city, query, urls)

    def _fetch_api_v2(self, city: str | None, query: str) -> SourceAdapterResult:
        # No `lang:en` filter — the product targets French / Pidgin / Swahili posts too.
        search_query = f"{query} -is:retweet"
        if city:
            search_query += f" {city}"
        if settings.x_languages:
            langs = " OR ".join(f"lang:{l}" for l in settings.split_csv(settings.x_languages))
            search_query += f" ({langs})"
        params = {
            "query": search_query[:512],
            "max_results": 50,
            "tweet.fields": "created_at,text,entities,author_id,geo",
            "expansions": "geo.place_id",
            "place.fields": "full_name,geo",
        }
        headers = {"Authorization": f"Bearer {settings.x_bearer_token}"}
        with self._client() as client:
            response = client.get("https://api.x.com/2/tweets/search/recent", headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        places = {p["id"]: p for p in (payload.get("includes") or {}).get("places", [])}
        items: list[RawSignalCreate] = []
        for tweet in payload.get("data", []):
            text = tweet.get("text", "")
            posted_at = parse_datetime(tweet.get("created_at"))
            start_time, end_time = infer_times_from_text(text)
            place = places.get(((tweet.get("geo") or {}).get("place_id")))
            loc = (place or {}).get("full_name") or city
            items.append(
                RawSignalCreate(
                    source_type="x",
                    source_name="x",
                    external_id=str(tweet.get("id")),
                    title=truncate(text.split("\n", 1)[0] or text, MAX_TITLE),
                    body=text,
                    location_text=truncate(loc, MAX_LOCATION),
                    url=f"https://x.com/i/web/status/{tweet.get('id')}",
                    posted_at=posted_at,
                    detected_start_time=start_time,
                    detected_end_time=end_time,
                    source_confidence=0.45,
                    normalized_category=infer_category(text),
                )
            )
        return SourceAdapterResult(items=items, fetched_count=len(items))


# ---------------------------------------------------------------------------
# Ticketmaster Discovery API — global, geo-queryable
# ---------------------------------------------------------------------------

_TM_SEGMENT_TO_CATEGORY = {
    "music": "culture", "arts & theatre": "culture", "film": "culture",
    "sports": "sports", "miscellaneous": None, "undefined": None,
}


class TicketmasterAdapter(BaseAdapter):
    """
    Ticketmaster Discovery API v2 — concerts, sports, theatre, family events in
    30+ countries. Free API key (developer.ticketmaster.com), 5 req/s.

    This is the backbone of "scan around me": it takes a lat/lng + radius
    directly, so it works for any location without per-city configuration.
    """
    source_name = "ticketmaster"
    supports_geo = True
    @property
    def API(self) -> str:
        return settings.ticketmaster_base_url.rstrip("/") + "/events.json"

    def is_configured(self) -> bool:
        return bool(settings.ticketmaster_api_key)

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        if not self.is_configured():
            if settings.enable_mock_adapters:
                return SourceAdapterResult(
                    items=[self._mock(city, query, "Mock Ticketmaster result. Set TICKETMASTER_API_KEY for live global discovery.", 0.6)],
                    fetched_count=1,
                )
            return SourceAdapterResult(items=[], fetched_count=0, errors=["ticketmaster: TICKETMASTER_API_KEY not set"])

        params: dict = {"apikey": settings.ticketmaster_api_key, "size": 100, "sort": "date,asc"}
        if geo:
            params["latlong"] = f"{geo.lat},{geo.lng}"
            params["radius"] = max(1, int(round(geo.radius_km)))
            params["unit"] = "km"
        elif city:
            params["city"] = city
        if query:
            params["keyword"] = query

        items: list[RawSignalCreate] = []
        errors: list[str] = []
        fetched = 0
        try:
            with self._client() as client:
                for page in range(5):  # 5 × 100 events per scan is plenty
                    params["page"] = page
                    r = client.get(self.API, params=params)
                    r.raise_for_status()
                    payload = r.json()
                    events = (payload.get("_embedded") or {}).get("events") or []
                    for raw in events:
                        fetched += 1
                        sig = self._to_signal(raw, city)
                        if sig:
                            items.append(sig)
                    total_pages = (payload.get("page") or {}).get("totalPages", 1)
                    if page + 1 >= total_pages or not events:
                        break
        except httpx.HTTPError as exc:
            errors.append(f"ticketmaster: {exc}")
            logger.warning("Ticketmaster fetch failed: %s", exc)
        return SourceAdapterResult(items=items, fetched_count=fetched, errors=errors)

    def _to_signal(self, raw: dict, city_hint: str | None) -> RawSignalCreate | None:
        name = raw.get("name")
        if not name:
            return None
        dates = raw.get("dates") or {}
        start = (dates.get("start") or {})
        start_dt = parse_datetime(start.get("dateTime") or start.get("localDate"))
        end_dt = parse_datetime(((dates.get("end") or {}).get("dateTime")))
        venues = (raw.get("_embedded") or {}).get("venues") or []
        venue = venues[0] if venues else {}
        loc = venue.get("location") or {}
        try:
            lat = float(loc["latitude"]) if loc.get("latitude") else None
            lng = float(loc["longitude"]) if loc.get("longitude") else None
        except (TypeError, ValueError):
            lat = lng = None
        venue_city = (venue.get("city") or {}).get("name")
        location_text = venue.get("name") or venue_city or city_hint
        if venue.get("name") and venue_city:
            location_text = f"{venue['name']}, {venue_city}"

        classifications = raw.get("classifications") or []
        segment = ((classifications[0].get("segment") or {}).get("name") or "").lower() if classifications else ""
        genre = ((classifications[0].get("genre") or {}).get("name") or "") if classifications else ""
        category = _TM_SEGMENT_TO_CATEGORY.get(segment) or infer_category(f"{name} {genre} {raw.get('info') or ''}")

        info = raw.get("info") or raw.get("pleaseNote") or ""
        body = f"{info}\n\n{segment.title()} · {genre}".strip() if (info or genre) else None
        price = (raw.get("priceRanges") or [{}])[0]
        if price.get("min") is not None:
            body = f"{body or ''}\n\nFrom {price.get('min')} {price.get('currency', '')}".strip()

        return RawSignalCreate(
            source_type="ticketmaster",
            source_name="ticketmaster",
            external_id=str(raw.get("id")),
            title=truncate(name, MAX_TITLE),
            body=body,
            location_text=truncate(location_text, MAX_LOCATION),
            url=raw.get("url"),
            detected_start_time=start_dt,
            detected_end_time=end_dt,
            latitude=lat,
            longitude=lng,
            source_confidence=0.92,   # ticketed, structured, verified listings
            normalized_category=category,
        )


# ---------------------------------------------------------------------------
# SeatGeek — US/CA/UK-centric, geo-queryable
# ---------------------------------------------------------------------------

_SG_TYPE_TO_CATEGORY = {
    "concert": "culture", "music_festival": "culture", "theater": "culture", "broadway_tickets_national": "culture",
    "comedy": "culture", "classical": "culture", "dance_performance_tour": "culture", "family": "community",
    "sports": "sports", "nba": "sports", "nfl": "sports", "mlb": "sports", "nhl": "sports", "mls": "sports",
    "soccer": "sports", "tennis": "sports", "boxing": "sports", "mma": "sports", "wrestling": "sports",
}


class SeatGeekAdapter(BaseAdapter):
    """
    SeatGeek Platform API — free client_id, lat/lon + range queries.
    Coverage is strongest in North America; included because it is cheap and
    complements Ticketmaster where both exist.
    """
    source_name = "seatgeek"
    supports_geo = True
    @property
    def API(self) -> str:
        return settings.seatgeek_base_url.rstrip("/") + "/events"

    def is_configured(self) -> bool:
        return bool(settings.seatgeek_client_id)

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None) -> SourceAdapterResult:
        if not self.is_configured():
            if settings.enable_mock_adapters:
                return SourceAdapterResult(
                    items=[self._mock(city, query, "Mock SeatGeek result. Set SEATGEEK_CLIENT_ID for live discovery.", 0.55)],
                    fetched_count=1,
                )
            return SourceAdapterResult(items=[], fetched_count=0, errors=["seatgeek: SEATGEEK_CLIENT_ID not set"])

        params: dict = {"client_id": settings.seatgeek_client_id, "per_page": 100, "sort": "datetime_utc.asc"}
        if geo:
            params.update({"lat": geo.lat, "lon": geo.lng, "range": f"{max(1, int(round(geo.radius_km)))}km"})
        elif city:
            params["venue.city"] = city
        if query:
            params["q"] = query

        items: list[RawSignalCreate] = []
        errors: list[str] = []
        fetched = 0
        try:
            with self._client() as client:
                for page in range(1, 4):
                    params["page"] = page
                    r = client.get(self.API, params=params)
                    r.raise_for_status()
                    payload = r.json()
                    events = payload.get("events") or []
                    for raw in events:
                        fetched += 1
                        sig = self._to_signal(raw, city)
                        if sig:
                            items.append(sig)
                    meta = payload.get("meta") or {}
                    if page * meta.get("per_page", 100) >= meta.get("total", 0) or not events:
                        break
        except httpx.HTTPError as exc:
            errors.append(f"seatgeek: {exc}")
            logger.warning("SeatGeek fetch failed: %s", exc)
        return SourceAdapterResult(items=items, fetched_count=fetched, errors=errors)

    def _to_signal(self, raw: dict, city_hint: str | None) -> RawSignalCreate | None:
        title = raw.get("title") or raw.get("short_title")
        if not title:
            return None
        venue = raw.get("venue") or {}
        loc = venue.get("location") or {}
        location_text = ", ".join(filter(None, [venue.get("name"), venue.get("city")])) or city_hint
        category = _SG_TYPE_TO_CATEGORY.get((raw.get("type") or "").lower()) or infer_category(title)
        stats = raw.get("stats") or {}
        body = None
        if stats.get("lowest_price"):
            body = f"From ${stats['lowest_price']}"
        return RawSignalCreate(
            source_type="seatgeek",
            source_name="seatgeek",
            external_id=str(raw.get("id")),
            title=truncate(title, MAX_TITLE),
            body=body,
            location_text=truncate(location_text, MAX_LOCATION),
            url=raw.get("url"),
            detected_start_time=parse_datetime(raw.get("datetime_utc")),
            latitude=loc.get("lat"),
            longitude=loc.get("lon"),
            source_confidence=0.88,
            normalized_category=category,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def build_adapters() -> dict[str, BaseAdapter]:
    return {
        "luma": LumaAdapter(),
        "eventbrite": EventbriteAdapter(),
        "telegram": FeedAdapter("telegram", settings.split_csv(settings.telegram_feed_urls)),
        "linkedin": LinkedInAdapter(settings.split_csv(settings.linkedin_source_urls)),
        "x": XAdapter(settings.split_csv(settings.x_feed_urls)),
        "ticketmaster": TicketmasterAdapter(),
        "seatgeek": SeatGeekAdapter(),
    }


ADAPTERS: dict[str, BaseAdapter] = build_adapters()


def ingest_from_source(
    source: str, city: str | None, query: str | None, urls: list[str] | None = None, geo: GeoQuery | None = None,
) -> SourceAdapterResult:
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise ValueError(f"Unsupported source: {source}. Known sources: {', '.join(sorted(ADAPTERS))}")
    return adapter.fetch(city=city, query=query, urls=urls, geo=geo)


def geo_sources() -> list[str]:
    """Names of adapters that can answer a GeoQuery."""
    return [name for name, a in ADAPTERS.items() if a.supports_geo]
