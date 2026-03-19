"""
Signal clustering service.

Scoring model
-------------
Each signal is scored against candidate events using four components:

  title_score    (0–1)   cosine similarity of embeddings, Jaccard fallback
  category_boost (0.1)   exact category match
  time_boost     (0.2)   start times within cluster_time_window_hours
  distance_boost (0.25)  venues within cluster_distance_km

  total ∈ [0, 1.55]

Decision thresholds:
  ≥ 0.55  → auto-link to best matching event
  0.35–0.54 → send to human review queue
  < 0.35  → create new event from signal

Candidate selection strategy
-----------------------------
On PostgreSQL + pgvector:
  Use an HNSW nearest-neighbour query (embedding <=> vec ORDER BY ... LIMIT k)
  to retrieve the top-k semantically similar events before scoring.
  This is O(k log n) in the database vs O(n) in Python — significant at scale.
  k defaults to PGVECTOR_CANDIDATE_K (20) which is more than enough given the
  additional time/category/geo re-ranking applied afterwards.

On SQLite / no pgvector / no signal embedding:
  Load all events and score every one — the original O(n) Python approach.
  Fine for dev/test and small deployments.

In both paths, events created during the current cluster pass are always
included as candidates so back-to-back signals about the same event link
correctly even before the next DB flush.
"""
from __future__ import annotations
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from app.core.config import settings
from app.models.event import Event, EventEvidence, RawSignal, ReviewQueueItem
from app.services.event_service import get_or_create_venue
from app.services.geo import haversine_km
from app.services import embeddings as emb_svc
from app.services.parsing import infer_structure_type
from app.services.scoring import infer_status, score_event
from app.services.webhooks import fire_event_confirmed

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
    if not (signal.latitude and signal.longitude and event.venue
            and event.venue.latitude and event.venue.longitude):
        return 0.0
    distance = haversine_km(
        signal.latitude, signal.longitude,
        event.venue.latitude, event.venue.longitude,
    )
    return DISTANCE_BOOST if distance <= settings.cluster_distance_km else 0.0


def _score(signal: RawSignal, event: Event) -> float:
    return (
        emb_svc.title_similarity(signal, event)
        + _category_similarity(signal, event)
        + _time_similarity(signal, event)
        + _geo_similarity(signal, event)
    )


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _get_candidates(
    db: Session,
    signal: RawSignal,
    newly_created: list[Event],
) -> list[Event]:
    """
    Return the candidate events to score against for a given signal.

    Tries pgvector nearest-neighbour first (fast, index-backed).
    Falls back to loading all events when pgvector is unavailable.

    Events created during the current cluster pass are always appended so
    a signal can match something seeded moments earlier in the same run.
    """
    candidates = emb_svc.nearest_events_pgvector(db, signal)

    if candidates is None:
        # SQLite / no pgvector / no embedding — full table scan
        candidates = list(
            db.execute(
                select(Event).options(
                    selectinload(Event.venue),
                    selectinload(Event.evidence),
                )
            ).scalars().all()
        )

    # Merge newly-created events that aren't yet in the DB result
    candidate_ids = {e.id for e in candidates}
    for e in newly_created:
        if e.id not in candidate_ids:
            candidates.append(e)

    return candidates


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cluster_signals(db: Session) -> tuple[list[int], list[int], list[int]]:
    created_ids: list[int] = []
    linked_ids:  list[int] = []
    queued_ids:  list[int] = []

    # Events seeded during this pass — kept in memory so later signals in the
    # same batch can match against them before they appear in DB queries.
    newly_created: list[Event] = []

    signals = db.execute(
        select(RawSignal).where(RawSignal.processed.is_(False))
    ).scalars().all()

    if not signals:
        return created_ids, linked_ids, queued_ids

    # Pre-fetch already-linked signal IDs in one query — avoids N+1
    already_linked_ids: set[int] = set(
        db.execute(
            select(EventEvidence.raw_signal_id).where(
                EventEvidence.raw_signal_id.in_([s.id for s in signals])
            )
        ).scalars().all()
    )

    for signal in signals:
        if signal.id in already_linked_ids:
            signal.processed = True
            continue

        # Back-fill embedding if the signal was ingested before the model was available
        if signal.embedding is None:
            emb_svc.embed_signal(db, signal)
            db.flush()

        candidates = _get_candidates(db, signal, newly_created)

        best_event: Event | None = None
        best_score = 0.0
        for event in candidates:
            s = _score(signal, event)
            if s > best_score:
                best_event = event
                best_score = s

        # ----------------------------------------------------------------
        # Decision
        # ----------------------------------------------------------------
        if best_event and best_score >= 0.55:
            db.add(EventEvidence(
                event_id=best_event.id,
                raw_signal_id=signal.id,
                weight=min(best_score, 1.0),
                evidence_type="cluster_match",
            ))
            evidence_count = len(best_event.evidence) + 1
            best_event.confidence_score = score_event(
                best_event.confidence_score, evidence_count,
                best_event.geo_precision_score, best_event.time_precision_score,
            )
            best_event.status = infer_status(
                best_event.start_time, best_event.end_time, best_event.confidence_score
            )
            signal.processed = True
            linked_ids.append(signal.id)
            fire_event_confirmed(
                event_id=best_event.id,
                title=best_event.title,
                status=best_event.status,
                confidence_score=best_event.confidence_score,
                category=best_event.category,
                start_time=best_event.start_time,
            )

        elif best_event and 0.35 <= best_score < 0.55:
            review = ReviewQueueItem(
                raw_signal_id=signal.id,
                candidate_event_id=best_event.id,
                reason="uncertain cluster match",
                score=best_score,
            )
            db.add(review)
            db.flush()
            queued_ids.append(review.id)

        else:
            venue = None
            if signal.location_text:
                venue = get_or_create_venue(db, name=signal.location_text, city=signal.location_text)
            event = Event(
                title=signal.title or "Untitled event",
                description=signal.body,
                category=signal.normalized_category or "general",
                structure_type=infer_structure_type(signal.source_type),
                geo_precision_score=0.75 if signal.latitude and signal.longitude else 0.35,
                time_precision_score=0.8 if signal.detected_start_time else 0.2,
                start_time=signal.detected_start_time,
                end_time=signal.detected_end_time,
                venue_id=venue.id if venue else None,
            )
            event.confidence_score = score_event(
                signal.source_confidence, 1,
                event.geo_precision_score, event.time_precision_score,
            )
            event.status = infer_status(event.start_time, event.end_time, event.confidence_score)
            db.add(event)
            db.flush()
            emb_svc.embed_event(db, event)
            db.add(EventEvidence(
                event_id=event.id,
                raw_signal_id=signal.id,
                weight=signal.source_confidence,
                evidence_type="seed_signal",
            ))
            signal.processed = True
            created_ids.append(event.id)
            newly_created.append(event)
            fire_event_confirmed(
                event_id=event.id,
                title=event.title,
                status=event.status,
                confidence_score=event.confidence_score,
                category=event.category,
                start_time=event.start_time,
            )

    db.commit()
    return created_ids, linked_ids, queued_ids
