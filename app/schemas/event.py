from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------

def _to_utc(value: datetime | None) -> datetime | None:
    """Naive datetimes are assumed UTC; aware ones are normalised to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _UTCDatetimes(BaseModel):
    """Mixin: every datetime field is coerced to aware UTC on input."""

    @field_validator("*", mode="after")
    @classmethod
    def _coerce_datetimes(cls, value):
        if isinstance(value, datetime):
            return _to_utc(value)
        return value


SourceType = Literal["luma", "eventbrite", "telegram", "linkedin", "x", "ticketmaster", "seatgeek"]
ReviewAction = Literal["approve_link", "reject", "discard", "recluster"]
ReviewStatus = Literal["pending", "approved", "rejected", "discarded", "reclustered"]
EventStatus = Literal["upcoming", "ongoing", "likely_ongoing", "ended", "uncertain"]


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class VenueIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    city: str | None = Field(default=None, max_length=255)
    country: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class EventCreate(_UTCDatetimes):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    category: str | None = Field(default=None, max_length=120)
    structure_type: str = Field(default="semi-structured", max_length=60)
    start_time: datetime | None = None
    end_time: datetime | None = None
    venue: VenueIn | None = None
    organizer_name: str | None = Field(default=None, max_length=255)


class EventUpdate(_UTCDatetimes):
    """Partial update — only provided fields change."""
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    category: str | None = Field(default=None, max_length=120)
    status: EventStatus | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    venue: VenueIn | None = None
    organizer_name: str | None = Field(default=None, max_length=255)


class RawSignalCreate(_UTCDatetimes):
    source_type: str = Field(min_length=1, max_length=120)
    source_name: str | None = Field(default=None, max_length=120)
    external_id: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    body: str | None = None
    location_text: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=1000)
    posted_at: datetime | None = None
    detected_start_time: datetime | None = None
    detected_end_time: datetime | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    source_confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    normalized_category: str | None = Field(default=None, max_length=120)


class IngestRequest(BaseModel):
    source: SourceType
    city: str | None = Field(default=None, max_length=255)
    query: str | None = Field(default=None, max_length=255)
    urls: list[str] | None = Field(default=None, max_length=25)
    # Geo-capable sources (ticketmaster, seatgeek) take a point + radius instead of a city
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0, le=500)

    @model_validator(mode="after")
    def _point_complete(self):
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be provided together")
        if self.radius_km is not None and self.lat is None:
            raise ValueError("radius_km requires lat and lng")
        return self

    @property
    def geo(self):
        from app.services.adapters import GeoQuery
        if self.lat is None:
            return None
        return GeoQuery(lat=self.lat, lng=self.lng, radius_km=self.radius_km or 25.0)

    @field_validator("urls")
    @classmethod
    def _only_http(cls, urls: list[str] | None) -> list[str] | None:
        if not urls:
            return urls
        cleaned = []
        for u in urls:
            u = u.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                raise ValueError(f"Only http(s) URLs are allowed: {u[:80]}")
            cleaned.append(u)
        return cleaned


class ReviewActionRequest(BaseModel):
    action: ReviewAction
    note: str | None = Field(default=None, max_length=500)
    candidate_event_id: int | None = None


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

class VenueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    address: str | None = None
    geo_source: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class OrganizerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category_focus: str | None = None
    reliability_score: float


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None = None
    category: str | None = None
    structure_type: str
    status: str
    confidence_score: float
    geo_precision_score: float
    time_precision_score: float
    start_time: datetime | None = None
    end_time: datetime | None = None
    venue: VenueOut | None = None
    organizer: OrganizerOut | None = None
    evidence_count: int = 0
    created_at: datetime
    updated_at: datetime


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_type: str
    source_name: str | None = None
    external_id: str | None = None
    title: str | None = None
    body: str | None = None
    location_text: str | None = None
    url: str | None = None
    posted_at: datetime | None = None
    detected_start_time: datetime | None = None
    detected_end_time: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_confidence: float
    normalized_category: str | None = None
    processed: bool
    ingested_at: datetime


class ReviewQueueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    raw_signal_id: int
    candidate_event_id: int | None = None
    reason: str
    score: float
    status: str
    resolution_note: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    # Both sides of the proposed match, so a reviewer can decide without extra calls
    signal: SignalOut | None = None
    candidate_event: EventOut | None = None


class SourceRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    city: str | None = None
    query: str | None = None
    status: str
    fetched_count: int
    created_signal_count: int
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ClusterResponse(BaseModel):
    created_event_ids: list[int]
    linked_signal_ids: list[int]
    queued_review_ids: list[int]
    remaining_unprocessed: int = 0


class ReviewResolveOut(BaseModel):
    id: int
    status: str
    event_id: int | None = None   # event linked or created as a result of the action


class IngestOut(BaseModel):
    run_id: int
    count: int
    fetched_count: int = 0
    signal_ids: list[int]


class Paginated(BaseModel):
    total: int
    page: int
    page_size: int


class PaginatedEventsOut(Paginated):
    items: list[EventOut]


class PaginatedSignalsOut(Paginated):
    items: list[SignalOut]


class PaginatedReviewQueueOut(Paginated):
    items: list[ReviewQueueOut]


class PaginatedSourceRunsOut(Paginated):
    items: list[SourceRunOut]


class CountBucket(BaseModel):
    key: str
    count: int


class StatsOut(BaseModel):
    categories: list[str] = []
    geo_sources: dict[str, bool] = {}     # geo-capable source → has credentials
    areas_covered: int = 0                # coverage cells scanned at least once
    events_total: int
    events_confirmed: int          # confidence >= confirmed threshold
    events_upcoming: int
    signals_total: int
    signals_unprocessed: int
    review_pending: int
    sources_last_run: dict[str, datetime | None]
    by_category: list[CountBucket]
    by_city: list[CountBucket]
    by_source: list[CountBucket]
