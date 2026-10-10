"""
Signal clustering service.

Scoring model
-------------
Each unprocessed signal is scored against candidate events using four components:

  title_score    (0–1)   cosine similarity of embeddings, Jaccard fallback
  category_boost (0.1)   exact category match
  time_boost     (0.2)   start times within cluster_time_window_hours
  distance_boost (0.25)  venues within cluster_distance_km

  total ∈ [0, 1.55]

  Hard vetoes come first: both sides geolocated and > CLUSTER_MAX_LINK_DISTANCE_KM
  apart, or both timed and > CLUSTER_MAX_LINK_TIME_DAYS apart → score 0.

  Category and distance boosts only apply once there is some title similarity
  (≥ 0.1) or a time match — otherwise every pair of events at one venue would
  land in the review band.

Decision thresholds (configurable, see Settings):
  ≥ CLUSTER_LINK_THRESHOLD (0.55)            → auto-link to best matching event
  CLUSTER_REVIEW_THRESHOLD (0.35) … link     → human review queue
  < CLUSTER_REVIEW_THRESHOLD                 → create a new event from the signal

Every branch marks the signal `processed = True`. A queued signal used to be
left unprocessed, so every subsequent pass re-scored it and opened *another*
review item — the queue filled with duplicates. Review actions that want the
signal re-evaluated ("recluster") flip `processed` back to False explicitly.

Candidate selection
-------------------
On PostgreSQL + pgvector: top-k nearest events by embedding (HNSW index).
Otherwise: all events, narrowed by a ±N-day window around the signal's
detected start time when it has one (events without a start_time are always
considered). Events created during the current pass are always appended so
back-to-back signals about the same thing link to each other.
"""
from __future__ import annotations
import logging
from datetime import timedelta

from sqlalchemy import or_, select, func
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models.event import Event, EventEvidence, RawSignal, ReviewQueueItem
from app.services import embeddings as emb_svc
from app.services.event_service import create_event_from_signal, link_signal_to_event
from app.services.geo import haversine_km
from app.services.webhooks import fire_if_newly_confirmed

logger = logging.getLogger(__name__)

CATEGORY_BOOST = 0.1
TIME_BOOST = 0.2
DISTANCE_BOOST = 0.25


# ---------------------------------------------------------------------------
# Component scorers
# ---------------------------------------------------------------------------

def _time_similarity(signal: RawSignal, event: Event) -> float:
    if not signal.detected_start_time or not event.start_time:
        return 0.0
    delta = abs(signal.detected_start_time - event.start_time)
    return TIME_BOOST if delta <= timedelta(hours=settings.cluster_time_window_hours) else 0.0


def _category_similarity(signal: RawSignal, event: Event) -> float:
    if signal.normalized_category and event.category and signal.normalized_category == event.category:
        return CATEGORY_BOOST
    return 0.0


def _geo_similarity(signal: RawSignal, event: Event) -> float:
    # `is not None` checks — a latitude/longitude of exactly 0.0 is valid
    # (the original truthiness test silently dropped equator/meridian points).
    if not signal.has_coordinates or event.venue is None or not event.venue.has_coordinates:
        return 0.0
    distance = haversine_km(signal.latitude, signal.longitude, event.venue.latitude, event.venue.longitude)
    return DISTANCE_BOOST if distance <= settings.cluster_distance_km else 0.0


# Boosts alone must never put a pair into the review band: two different
# concerts at the same venue share category + location and would otherwise
# score 0.35 every time, flooding the queue. Require a little title or time
# evidence before location/category count for anything.
MIN_TITLE_EVIDENCE = 0.1


def _vetoed(signal: RawSignal, event: Event) -> bool:
    """Same-looking events in different cities / on different weeks are different events."""
    if signal.has_coordinates and event.venue is not None and event.venue.has_coordinates:
        if haversine_km(signal.latitude, signal.longitude, event.venue.latitude, event.venue.longitude) > settings.cluster_max_link_distance_km:
            return True
    if signal.detected_start_time and event.start_time:
        if abs(signal.detected_start_time - event.start_time) > timedelta(days=settings.cluster_max_link_time_days):
            return True
    return False


def score_pair(signal: RawSignal, event: Event) -> float:
    if _vetoed(signal, event):
        return 0.0
    title = emb_svc.title_similarity(signal, event)
    time_boost = _time_similarity(signal, event)
    if title < MIN_TITLE_EVIDENCE and time_boost == 0.0:
        return title
    return title + _category_similarity(signal, event) + time_boost + _geo_similarity(signal, event)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _get_candidates(db: Session, signal: RawSignal, newly_created: list[Event]) -> list[Event]:
    candidates = emb_svc.nearest_events_pgvector(db, signal)

    if candidates is None:
        stmt = select(Event).options(selectinload(Event.venue), selectinload(Event.evidence), selectinload(Event.organizer))
        if signal.detected_start_time is not None:
            window = timedelta(days=settings.cluster_candidate_window_days)
            lo, hi = signal.detected_start_time - window, signal.detected_start_time + window
            stmt = stmt.where(or_(Event.start_time.is_(None), Event.start_time.between(lo, hi)))
        candidates = list(db.execute(stmt).scalars().all())

    candidate_ids = {e.id for e in candidates}
    for e in newly_created:
        if e.id not in candidate_ids:
            candidates.append(e)
    return candidates


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cluster_signals(db: Session, limit: int | None = None) -> tuple[list[int], list[int], list[int]]:
    """
    Process up to `limit` unprocessed signals (default: settings.cluster_batch_size).
    Returns (created_event_ids, linked_signal_ids, queued_review_ids).
    """
    created_ids: list[int] = []
    linked_ids: list[int] = []
    queued_ids: list[int] = []
    newly_created: list[Event] = []

    batch = limit or settings.cluster_batch_size
    signals = db.execute(
        select(RawSignal)
        .where(RawSignal.processed.is_(False))
        .order_by(RawSignal.ingested_at.asc(), RawSignal.id.asc())
        .limit(batch)
    ).scalars().all()

    if not signals:
        return created_ids, linked_ids, queued_ids

    # Signals already attached to an event (e.g. via manual review) just need flagging
    already_linked_ids: set[int] = set(
        db.execute(
            select(EventEvidence.raw_signal_id).where(EventEvidence.raw_signal_id.in_([s.id for s in signals]))
        ).scalars().all()
    )
    # Signals that already have an *open* review item must not be re-queued
    pending_review_ids: set[int] = set(
        db.execute(
            select(ReviewQueueItem.raw_signal_id).where(
                ReviewQueueItem.raw_signal_id.in_([s.id for s in signals]),
                ReviewQueueItem.status == "pending",
            )
        ).scalars().all()
    )

    for signal in signals:
        if signal.id in already_linked_ids or signal.id in pending_review_ids:
            signal.processed = True
            continue

        if signal.embedding is None:
            emb_svc.embed_signal(db, signal)

        best_event: Event | None = None
        best_score = 0.0
        for event in _get_candidates(db, signal, newly_created):
            s = score_pair(signal, event)
            if s > best_score:
                best_event, best_score = event, s

        if best_event is not None and best_score >= settings.cluster_link_threshold:
            before, after = link_signal_to_event(
                db, signal, best_event, weight=best_score, evidence_type="cluster_match",
            )
            linked_ids.append(signal.id)
            fire_if_newly_confirmed(best_event, before, after)

        elif best_event is not None and best_score >= settings.cluster_review_threshold:
            review = ReviewQueueItem(
                raw_signal_id=signal.id,
                candidate_event_id=best_event.id,
                reason=f"uncertain cluster match (score {best_score:.2f})",
                score=round(best_score, 3),
            )
            db.add(review)
            signal.processed = True
            db.flush()
            queued_ids.append(review.id)

        else:
            event = create_event_from_signal(db, signal)
            created_ids.append(event.id)
            newly_created.append(event)
            fire_if_newly_confirmed(event, 0.0, event.confidence_score)

    db.commit()
    logger.info(
        "cluster pass: %d signals → %d new events, %d linked, %d queued",
        len(signals), len(created_ids), len(linked_ids), len(queued_ids),
    )
    return created_ids, linked_ids, queued_ids


def count_unprocessed(db: Session) -> int:
    return db.execute(select(func.count()).select_from(RawSignal).where(RawSignal.processed.is_(False))).scalar_one()
