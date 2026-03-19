from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.event import Event, EventEvidence, Organizer, ReviewQueueItem
from app.services.clustering import cluster_signals
from app.services.scoring import infer_status, score_event
from app.services.webhooks import fire_event_confirmed

# How much to shift an organizer's reliability score per resolved review
_RELIABILITY_APPROVE_DELTA = 0.03
_RELIABILITY_REJECT_DELTA = 0.05  # penalise a bit more than reward
_RELIABILITY_MIN = 0.0
_RELIABILITY_MAX = 1.0


def _update_organizer_reliability(db: Session, event: Event, approved: bool) -> None:
    """Nudge the organizer's reliability score based on a manual review outcome."""
    if event.organizer_id is None:
        return
    organizer = db.execute(select(Organizer).where(Organizer.id == event.organizer_id)).scalar_one_or_none()
    if organizer is None:
        return
    if approved:
        organizer.reliability_score = min(organizer.reliability_score + _RELIABILITY_APPROVE_DELTA, _RELIABILITY_MAX)
    else:
        organizer.reliability_score = max(organizer.reliability_score - _RELIABILITY_REJECT_DELTA, _RELIABILITY_MIN)
    db.add(organizer)


def resolve_review_item(
    db: Session,
    item_id: int,
    action: str,
    note: str | None = None,
    candidate_event_id: int | None = None,
):
    item = db.execute(select(ReviewQueueItem).where(ReviewQueueItem.id == item_id)).scalar_one_or_none()
    if item is None:
        raise ValueError("Review item not found")
    if item.status != "pending":
        raise ValueError("Review item already resolved")

    if action == "approve_link":
        target_event_id = candidate_event_id or item.candidate_event_id
        if not target_event_id:
            raise ValueError("candidate_event_id is required for approve_link")
        event = db.execute(select(Event).where(Event.id == target_event_id)).scalar_one_or_none()
        if event is None:
            raise ValueError("Target event not found")
        db.add(EventEvidence(
            event_id=event.id,
            raw_signal_id=item.raw_signal_id,
            weight=max(item.score, 0.35),
            evidence_type="manual_review_link",
        ))
        event.confidence_score = score_event(
            min(event.confidence_score + 0.05, 1.0),
            len(event.evidence) + 1,
            event.geo_precision_score,
            event.time_precision_score,
        )
        event.status = infer_status(event.start_time, event.end_time, event.confidence_score)
        _update_organizer_reliability(db, event, approved=True)
        item.status = "approved"
        fire_event_confirmed(
            event_id=event.id,
            title=event.title,
            status=event.status,
            confidence_score=event.confidence_score,
            category=event.category,
            start_time=event.start_time,
        )

    elif action == "reject":
        # If the signal was tentatively associated with an event, penalise that
        # event's organizer for the false positive.
        if item.candidate_event_id:
            event = db.execute(select(Event).where(Event.id == item.candidate_event_id)).scalar_one_or_none()
            if event:
                _update_organizer_reliability(db, event, approved=False)
        item.status = "rejected"

    elif action == "recluster":
        item.status = "reclustered"
        item.resolution_note = note
        item.resolved_at = datetime.now(timezone.utc)
        db.add(item)
        db.commit()
        cluster_signals(db)
        return {"id": item.id, "status": item.status}

    else:
        raise ValueError("Unsupported action")

    item.resolution_note = note
    item.resolved_at = datetime.now(timezone.utc)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "status": item.status}
