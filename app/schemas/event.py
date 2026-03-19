from datetime import datetime
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class VenueIn(BaseModel):
    name: str
    city: str | None = None
    country: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class EventCreate(BaseModel):
    title: str
    description: str | None = None
    category: str | None = None
    structure_type: str = "semi-structured"
    start_time: datetime | None = None
    end_time: datetime | None = None
    venue: VenueIn | None = None
    organizer_name: str | None = None


class RawSignalCreate(BaseModel):
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
    source_confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    normalized_category: str | None = None


class IngestRequest(BaseModel):
    source: str
    city: str | None = None
    query: str | None = None
    urls: list[str] | None = None


class ReviewActionRequest(BaseModel):
    action: str
    note: str | None = None
    candidate_event_id: int | None = None


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

class VenueOut(BaseModel):
    id: int
    name: str
    city: str | None = None
    country: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    model_config = {"from_attributes": True}


class OrganizerOut(BaseModel):
    id: int
    name: str
    category_focus: str | None = None
    reliability_score: float

    model_config = {"from_attributes": True}


class EventOut(BaseModel):
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
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SignalOut(BaseModel):
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

    model_config = {"from_attributes": True}


class ReviewQueueOut(BaseModel):
    id: int
    raw_signal_id: int
    candidate_event_id: int | None = None
    reason: str
    score: float
    status: str
    resolution_note: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    model_config = {"from_attributes": True}


class SourceRunOut(BaseModel):
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

    model_config = {"from_attributes": True}


class ClusterResponse(BaseModel):
    created_event_ids: list[int]
    linked_signal_ids: list[int]
    queued_review_ids: list[int]


class ReviewResolveOut(BaseModel):
    id: int
    status: str


class IngestOut(BaseModel):
    run_id: int
    count: int
    signal_ids: list[int]


class PaginatedEventsOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[EventOut]


class PaginatedSignalsOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[SignalOut]


class PaginatedReviewQueueOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ReviewQueueOut]
