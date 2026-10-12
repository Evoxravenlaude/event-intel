from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base
from app.db.types import EmbeddingType, UTCDateTime


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Venue(Base):
    __tablename__ = "venues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # Where the coordinates came from: source | google | nominatim | geocoder | gazetteer | manual
    geo_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # How precise they are: exact | street | area | city | region
    geo_accuracy: Mapped[str | None] = mapped_column(String(10), nullable=True)
    neighbourhood: Mapped[str | None] = mapped_column(String(255), nullable=True)

    aliases = relationship("VenueAlias", back_populates="venue", cascade="all, delete-orphan")

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class VenueAlias(Base):
    __tablename__ = "venue_aliases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    venue = relationship("Venue", back_populates="aliases")


class Organizer(Base):
    __tablename__ = "organizers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    category_focus: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.5)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    structure_type: Mapped[str] = mapped_column(String(60), default="semi-structured")
    status: Mapped[str] = mapped_column(String(60), default="uncertain", index=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    geo_precision_score: Mapped[float] = mapped_column(Float, default=0.0)
    time_precision_score: Mapped[float] = mapped_column(Float, default=0.0)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id"), nullable=True, index=True)
    organizer_id: Mapped[int | None] = mapped_column(ForeignKey("organizers.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)
    embedding: Mapped[list | None] = mapped_column(EmbeddingType, nullable=True)

    venue = relationship("Venue")
    organizer = relationship("Organizer")
    evidence = relationship("EventEvidence", back_populates="event", cascade="all, delete-orphan")

    @property
    def evidence_count(self) -> int:
        return len(self.evidence) if self.evidence is not None else 0

    @property
    def source_url(self) -> str | None:
        """Link to the original listing (first evidence signal that has one)."""
        for ev in sorted(self.evidence or [], key=lambda e: e.id or 0):
            rs = ev.raw_signal
            if rs is not None and rs.url:
                return rs.url
        return None


class RawSignal(Base):
    __tablename__ = "raw_signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(120), index=True)
    source_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    detected_start_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    detected_end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_confidence: Mapped[float] = mapped_column(Float, default=0.4)
    normalized_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    embedding: Mapped[list | None] = mapped_column(EmbeddingType, nullable=True)

    __table_args__ = (UniqueConstraint("source_type", "external_id", name="uq_raw_signal_source_external"),)

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class EventEvidence(Base):
    __tablename__ = "event_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    raw_signal_id: Mapped[int] = mapped_column(ForeignKey("raw_signals.id"), index=True)
    weight: Mapped[float] = mapped_column(Float, default=0.2)
    evidence_type: Mapped[str] = mapped_column(String(120), default="mention")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    event = relationship("Event", back_populates="evidence")
    raw_signal = relationship("RawSignal")


class ReviewQueueItem(Base):
    __tablename__ = "review_queue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_signal_id: Mapped[int] = mapped_column(ForeignKey("raw_signals.id"), index=True)
    candidate_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(255))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(60), default="pending", index=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Reviewers need to see both sides of the proposed match.
    signal = relationship("RawSignal", foreign_keys=[raw_signal_id])
    candidate_event = relationship("Event", foreign_keys=[candidate_event_id])


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(120), index=True)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    query: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(60), default="started", index=True)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    created_signal_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class GeocodeCache(Base):
    """One row per distinct geocoding query (forward or reverse). NULL result = cached miss."""
    __tablename__ = "geocode_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    query: Mapped[str] = mapped_column(String(500))
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class CoverageCell(Base):
    """
    A scanned area: a circle (centre + radius_km) keyed by the ~1 km geohash
    cell of its centre. `/discover` checks whether a request circle is
    contained in a recently scanned circle before running sources again.
    """
    __tablename__ = "coverage_cells"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cell: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    latitude: Mapped[float] = mapped_column(Float, index=True)
    longitude: Mapped[float] = mapped_column(Float, index=True)
    radius_km: Mapped[float] = mapped_column(Float, default=25.0)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    scan_count: Mapped[int] = mapped_column(Integer, default=0)
    last_signal_count: Mapped[int] = mapped_column(Integer, default=0)
    last_scanned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    last_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
