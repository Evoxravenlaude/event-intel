from __future__ import annotations
from dataclasses import dataclass
from datetime import timezone
import json
import re
from typing import Iterable

import feedparser
import httpx
from bs4 import BeautifulSoup
from app.core.config import settings
from app.schemas.event import RawSignalCreate
from app.services.parsing import build_external_id, html_to_signal, infer_category, parse_datetime, infer_times_from_text


@dataclass
class SourceAdapterResult:
    items: list[RawSignalCreate]
    fetched_count: int


class BaseAdapter:
    source_name: str

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
        raise NotImplementedError

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=settings.source_timeout_seconds, follow_redirects=True)


class LumaAdapter(BaseAdapter):
    source_name = "luma"

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
        feed_urls = urls or settings.split_csv(settings.luma_feed_urls)
        items: list[RawSignalCreate] = []
        fetched_count = 0
        for url in feed_urls:
            if url.endswith(".ics"):
                items.extend(self._fetch_ics(url))
                fetched_count += 1
            else:
                with self._client() as client:
                    response = client.get(url)
                    response.raise_for_status()
                    items.append(html_to_signal("luma", "luma", url, response.text, city))
                    fetched_count += 1
        return SourceAdapterResult(items=items, fetched_count=fetched_count)

    def _fetch_ics(self, url: str) -> list[RawSignalCreate]:
        from icalendar import Calendar
        with self._client() as client:
            response = client.get(url)
            response.raise_for_status()
        # Use bytes — icalendar expects bytes to handle encoding correctly
        calendar = Calendar.from_ical(response.content)
        items: list[RawSignalCreate] = []
        for component in calendar.walk():
            if component.name != "VEVENT":
                continue
            start = component.get("dtstart")
            end = component.get("dtend")
            title = str(component.get("summary", "Untitled event"))
            description = str(component.get("description", ""))
            location_text = str(component.get("location", "")) or None
            start_dt = start.dt if start else None
            end_dt = end.dt if end else None
            if hasattr(start_dt, "tzinfo") and start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if hasattr(end_dt, "tzinfo") and end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            items.append(
                RawSignalCreate(
                    source_type="luma",
                    source_name="luma",
                    external_id=build_external_id(url, title + str(start_dt)),
                    title=title,
                    body=description,
                    location_text=location_text,
                    url=url,
                    detected_start_time=start_dt,
                    detected_end_time=end_dt,
                    source_confidence=0.86,
                    normalized_category=infer_category(f"{title} {description}"),
                )
            )
        return items


class EventbriteAdapter(BaseAdapter):
    source_name = "eventbrite"

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
        items: list[RawSignalCreate] = []
        fetched_count = 0
        if settings.eventbrite_private_token and query:
            headers = {"Authorization": f"Bearer {settings.eventbrite_private_token}"}
            params = {"q": query}
            if city:
                params["location.address"] = city
            with self._client() as client:
                response = client.get("https://www.eventbriteapi.com/v3/events/search/", headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
                for raw in payload.get("events", []):
                    items.append(
                        RawSignalCreate(
                            source_type="eventbrite",
                            source_name="eventbrite",
                            external_id=str(raw.get("id")),
                            title=raw.get("name", {}).get("text"),
                            body=raw.get("description", {}).get("text"),
                            location_text=city,
                            url=raw.get("url"),
                            detected_start_time=parse_datetime(raw.get("start", {}).get("utc")),
                            detected_end_time=parse_datetime(raw.get("end", {}).get("utc")),
                            source_confidence=0.9,
                            normalized_category=infer_category(json.dumps(raw)),
                        )
                    )
                fetched_count += len(payload.get("events", []))
                return SourceAdapterResult(items=items, fetched_count=fetched_count)

        for url in urls or []:
            with self._client() as client:
                response = client.get(url)
                response.raise_for_status()
                items.append(html_to_signal("eventbrite", "eventbrite", url, response.text, city))
                fetched_count += 1
        if not items and settings.enable_mock_adapters:
            items.append(
                RawSignalCreate(
                    source_type="eventbrite",
                    source_name="eventbrite",
                    external_id=build_external_id(None, f"{query or 'Eventbrite'}-{city or 'global'}"),
                    title=f"{query or 'Professional event'} in {city or 'Global'}",
                    body="Mock Eventbrite result. Add EVENTBRITE_PRIVATE_TOKEN or provide page URLs for live fetches.",
                    location_text=city,
                    source_confidence=0.58,
                    normalized_category=infer_category(query or "professional event"),
                )
            )
            fetched_count = 1
        return SourceAdapterResult(items=items, fetched_count=fetched_count)


class LinkedInAdapter(BaseAdapter):
    """
    LinkedIn adapter.

    LinkedIn has no public event API. This adapter handles two realistic sources:

    1. **RSS/Atom feed URLs** — company page update feeds or third-party scrapers.
       Configured via LINKEDIN_SOURCE_URLS (comma-separated).
    2. **Direct page URLs** in the ingest payload — fetches the HTML and
       extracts structured event data from LinkedIn's JSON-LD embedding.

    JSON-LD extraction catches the most signal: title, description, start/end
    dates, and location are all present in LinkedIn's structured markup when
    an event page is fetched without auth (public events only).
    """

    source_name = "linkedin"

    def __init__(self, configured_urls: list[str]):
        self.configured_urls = configured_urls

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
        items: list[RawSignalCreate] = []
        fetched_count = 0

        target_urls = urls or self.configured_urls
        feed_urls = [u for u in target_urls if not u.startswith("https://www.linkedin.com/events/")]
        event_page_urls = [u for u in target_urls if u.startswith("https://www.linkedin.com/events/")]

        # Path 1: RSS/Atom feed URLs
        if feed_urls:
            feed_result = FeedAdapter("linkedin", feed_urls).fetch(city, query, None)
            items.extend(feed_result.items)
            fetched_count += feed_result.fetched_count

        # Path 2: LinkedIn event page URLs → JSON-LD extraction
        for url in event_page_urls:
            signal = self._fetch_event_page(url, city)
            if signal:
                items.append(signal)
                fetched_count += 1

        if not items and settings.enable_mock_adapters:
            items.append(
                RawSignalCreate(
                    source_type="linkedin",
                    source_name="linkedin",
                    external_id=build_external_id(None, f"linkedin-{city or 'global'}"),
                    title=f"{query or 'Professional event'} in {city or 'Global'}",
                    body=(
                        "Mock LinkedIn result. Provide LINKEDIN_SOURCE_URLS (RSS/Atom) "
                        "or pass LinkedIn event page URLs in the ingest payload."
                    ),
                    location_text=city,
                    source_confidence=0.5,
                    normalized_category=infer_category(query or "business"),
                )
            )
            fetched_count = max(fetched_count, 1)

        return SourceAdapterResult(items=items, fetched_count=fetched_count)

    def _fetch_event_page(self, url: str, city: str | None) -> RawSignalCreate | None:
        """
        Fetch a LinkedIn event page and extract data from JSON-LD and meta tags.
        LinkedIn embeds structured event data in <script type="application/ld+json">
        for public events.
        """
        import json as _json
        try:
            with self._client() as client:
                response = client.get(url)
                response.raise_for_status()
        except Exception:
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        # Try JSON-LD first (most reliable)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = _json.loads(script.string or "")
                if isinstance(data, list):
                    data = next((d for d in data if d.get("@type") in ("Event", "SocialEvent")), None)
                if not data or data.get("@type") not in ("Event", "SocialEvent"):
                    continue

                title = data.get("name")
                description = data.get("description")
                start_dt = parse_datetime(data.get("startDate"))
                end_dt = parse_datetime(data.get("endDate"))
                location = data.get("location", {})
                location_text = (
                    location.get("name")
                    or location.get("address", {}).get("addressLocality")
                    or city
                )
                latitude = location.get("geo", {}).get("latitude")
                longitude = location.get("geo", {}).get("longitude")

                return RawSignalCreate(
                    source_type="linkedin",
                    source_name="linkedin",
                    external_id=build_external_id(url, title),
                    title=title,
                    body=description,
                    location_text=location_text,
                    url=url,
                    detected_start_time=start_dt,
                    detected_end_time=end_dt,
                    latitude=float(latitude) if latitude else None,
                    longitude=float(longitude) if longitude else None,
                    source_confidence=0.78,  # JSON-LD from official page = high confidence
                    normalized_category=infer_category(f"{title} {description}"),
                )
            except (ValueError, KeyError, TypeError):
                continue

        # Fallback: plain HTML extraction
        return html_to_signal("linkedin", "linkedin", url, response.text, city)
        self.source_name = source_name
        self.configured_urls = configured_urls

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
        target_urls = urls or self.configured_urls
        items: list[RawSignalCreate] = []
        fetched_count = 0
        for url in target_urls:
            feed = feedparser.parse(url)
            fetched_count += 1
            for entry in feed.entries:
                title = entry.get("title")
                body = re.sub("<[^>]+>", " ", entry.get("summary", "") or entry.get("description", ""))
                full_text = f"{title or ''} {body or ''}"
                items.append(
                    RawSignalCreate(
                        source_type=self.source_name,
                        source_name=self.source_name,
                        external_id=entry.get("id") or build_external_id(entry.get("link"), title),
                        title=title,
                        body=body,
                        location_text=city,
                        url=entry.get("link"),
                        posted_at=parse_datetime(entry.get("published")),
                        source_confidence=0.63,
                        normalized_category=infer_category(full_text),
                    )
                )
        if not items and settings.enable_mock_adapters:
            items.append(
                RawSignalCreate(
                    source_type=self.source_name,
                    source_name=self.source_name,
                    external_id=build_external_id(None, f"{self.source_name}-{city or 'global'}"),
                    title=f"{query or self.source_name.title()} event in {city or 'Global'}",
                    body=f"Mock {self.source_name} result. Add feed URLs in env or payload urls.",
                    location_text=city,
                    source_confidence=0.5,
                    normalized_category=infer_category(query or self.source_name),
                )
            )
            fetched_count = max(fetched_count, 1)
        return SourceAdapterResult(items=items, fetched_count=fetched_count)


class XAdapter(BaseAdapter):
    """
    X (Twitter) adapter.

    Priority order:
    1. X API v2 recent-search endpoint when X_BEARER_TOKEN is configured.
    2. Feed URLs from env / payload (RSS/Atom bridges like nitter.net).
    3. Mock item when ENABLE_MOCK_ADAPTERS is true and nothing else yielded results.
    """

    source_name = "x"

    def __init__(self, configured_urls: list[str] | None = None):
        self.configured_urls = configured_urls or []

    def fetch(self, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
        if settings.x_bearer_token and query:
            return self._fetch_api_v2(city, query)
        # Fall back to feed-based ingestion
        return FeedAdapter("x", self.configured_urls).fetch(city, query, urls)

    def _fetch_api_v2(self, city: str | None, query: str) -> SourceAdapterResult:
        search_query = f"{query} lang:en -is:retweet"
        if city:
            search_query += f" {city}"
        params = {
            "query": search_query,
            "max_results": 20,
            "tweet.fields": "created_at,text,entities,author_id",
        }
        headers = {"Authorization": f"Bearer {settings.x_bearer_token}"}
        with self._client() as client:
            response = client.get(
                "https://api.twitter.com/2/tweets/search/recent",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            payload = response.json()

        items: list[RawSignalCreate] = []
        for tweet in payload.get("data", []):
            text = tweet.get("text", "")
            created_raw = tweet.get("created_at")
            posted_at = parse_datetime(created_raw)
            start_time, end_time = infer_times_from_text(text)
            items.append(
                RawSignalCreate(
                    source_type="x",
                    source_name="x",
                    external_id=str(tweet.get("id")),
                    title=text[:255],
                    body=text,
                    location_text=city,
                    url=f"https://x.com/i/web/status/{tweet.get('id')}",
                    posted_at=posted_at,
                    detected_start_time=start_time,
                    detected_end_time=end_time,
                    # Tweets are lower confidence than structured sources
                    source_confidence=0.45,
                    normalized_category=infer_category(text),
                )
            )
        return SourceAdapterResult(items=items, fetched_count=len(items))


ADAPTERS: dict[str, BaseAdapter] = {
    "luma": LumaAdapter(),
    "eventbrite": EventbriteAdapter(),
    "telegram": FeedAdapter("telegram", settings.split_csv(settings.telegram_feed_urls)),
    "linkedin": LinkedInAdapter(settings.split_csv(settings.linkedin_source_urls)),
    "x": XAdapter(),
}


def ingest_from_source(source: str, city: str | None, query: str | None, urls: list[str] | None = None) -> SourceAdapterResult:
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise ValueError(f"Unsupported source: {source}")
    return adapter.fetch(city=city, query=query, urls=urls)
