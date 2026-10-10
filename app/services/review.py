"""
Human review of uncertain cluster matches.

Actions
-------
approve_link   The signal IS about the candidate event (or `candidate_event_id`
               if the reviewer picked a different one). Link it as evidence,
               rescore, reward the organizer.
reject         The signal is a real event but NOT the candidate. Seed a new
               event from it so nothing is lost; penalise the candidate's
               organizer for the near-miss.
discard        The signal is noise (spam, not an event). Mark processed, link
               nothing, create nothing.
recluster      Re-run clustering for this signal (e.g. after fixing the
               candidate event's data). The signal is flagged unprocessed and
               a cluster pass runs immediately.

Every action leaves the signal `processed` consistent with the outcome so the
next cluster pass does not re-open the same question.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event import Event, Organizer, RawSignal, ReviewQueueItem
from app.services.clustering import cluster_signals
from app.services.event_service import create_event_from_signal, link_signal_to_event
from app.services.webhooks import fire_if_newly_confirmed

# How much to shift an organizer's reliability score per resolved review
_RELIABILITY_APPROVE_DELTA = 0.03
_RELIABILITY_REJECT_DELTA = 0.05  # penalise a bit more than reward
_RELIABILITY_MIN = 0.0
_RELIABILITY_MAX = 1.0

VALID_ACTIONS = ("approve_link", "reject", "discard", "recluster")


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
) -> dict:
    item = db.execute(select(ReviewQueueItem).where(ReviewQueueItem.id == item_id)).scalar_one_or_none()
    if item is None:
        raise LookupError("Review item not found")
    if item.status != "pending":
        raise ValueError(f"Review item already resolved ({item.status})")
    if action not in VALID_ACTIONS:
        raise ValueError(f"Unsupported action '{action}'. Valid actions: {', '.join(VALID_ACTIONS)}")

    signal = db.execute(select(RawSignal).where(RawSignal.id == item.raw_signal_id)).scalar_one_or_none()
    if signal is None:
        raise ValueError("Signal for this review item no longer exists")

    result_event_id: int | None = None

    if action == "approve_link":
        target_event_id = candidate_event_id or item.candidate_event_id
        if not target_event_id:
            raise ValueError("candidate_event_id is required for approve_link")
        event = db.execute(select(Event).where(Event.id == target_event_id)).scalar_one_or_none()
        if event is None:
            raise ValueError("Target event not found")
        before, after = link_signal_to_event(
            db, signal, event, weight=max(item.score, 0.35), evidence_type="manual_review_link",
        )
        _update_organizer_reliability(db, event, approved=True)
        fire_if_newly_confirmed(event, before, after)
        item.status = "approved"
        item.candidate_event_id = event.id
        result_event_id = event.id

    elif action == "reject":
        if item.candidate_event_id:
            candidate = db.execute(select(Event).where(Event.id == item.candidate_event_id)).scalar_one_or_none()
            if candidate:
                _update_organizer_reliability(db, candidate, approved=False)
        new_event = create_event_from_signal(db, signal, evidence_type="manual_review_seed")
        fire_if_newly_confirmed(new_event, 0.0, new_event.confidence_score)
        item.status = "rejected"
        result_event_id = new_event.id

    elif action == "discard":
        signal.processed = True
        item.status = "discarded"

    elif action == "recluster":
        signal.processed = False
        item.status = "reclustered"

    item.resolution_note = note
    item.resolved_at = datetime.now(timezone.utc)
    db.add(item)
    db.commit()

    if action == "recluster":
        cluster_signals(db)
        # Report what became of the signal: the event it now evidences, if any
        from app.models.event import EventEvidence
        result_event_id = db.execute(
            select(EventEvidence.event_id)
            .where(EventEvidence.raw_signal_id == signal.id)
            .order_by(EventEvidence.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    db.refresh(item)
    return {"id": item.id, "status": item.status, "event_id": result_event_id}
