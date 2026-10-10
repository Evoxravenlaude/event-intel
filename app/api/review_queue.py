from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.session import get_db
from app.models.event import Event, ReviewQueueItem
from app.schemas.event import PaginatedReviewQueueOut, ReviewActionRequest, ReviewResolveOut, ReviewQueueOut
from app.services.review import resolve_review_item

router = APIRouter(prefix="/review-queue", tags=["review-queue"])


def _review_query():
    return select(ReviewQueueItem).options(
        selectinload(ReviewQueueItem.signal),
        selectinload(ReviewQueueItem.candidate_event).selectinload(Event.venue),
        selectinload(ReviewQueueItem.candidate_event).selectinload(Event.organizer),
        selectinload(ReviewQueueItem.candidate_event).selectinload(Event.evidence),
    )


@router.get("", response_model=PaginatedReviewQueueOut)
def list_review_queue(
    db: Session = Depends(get_db),
    status: str | None = Query(default="pending", description="pending | approved | rejected | discarded | reclustered | all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    query = _review_query().order_by(ReviewQueueItem.created_at.desc(), ReviewQueueItem.id.desc())
    if status and status != "all":
        query = query.where(ReviewQueueItem.status == status)

    total = db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    items = list(db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all())
    return PaginatedReviewQueueOut(total=total, page=page, page_size=page_size, items=items)


@router.get("/{item_id}", response_model=ReviewQueueOut)
def get_review_item(item_id: int, db: Session = Depends(get_db)):
    item = db.execute(_review_query().where(ReviewQueueItem.id == item_id)).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    return item


@router.post("/{item_id}/resolve", response_model=ReviewResolveOut)
def resolve_queue_item(item_id: int, payload: ReviewActionRequest, db: Session = Depends(get_db)):
    try:
        return resolve_review_item(
            db,
            item_id=item_id,
            action=payload.action,
            note=payload.note,
            candidate_event_id=payload.candidate_event_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
