from __future__ import annotations
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.event import RawSignal, SourceRun
from app.schemas.event import (
    ClusterResponse,
    IngestOut,
    IngestRequest,
    PaginatedSignalsOut,
    PaginatedSourceRunsOut,
    RawSignalCreate,
    SignalOut,
)
from app.services.adapters import ingest_from_source
from app.services.clustering import cluster_signals, count_unprocessed
from app.services.event_service import create_signal, create_source_run, finish_source_run

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/signals", tags=["signals"])


@router.post("", response_model=SignalOut, status_code=status.HTTP_201_CREATED)
def post_signal(payload: RawSignalCreate, db: Session = Depends(get_db)):
    return create_signal(db, payload)


@router.get("", response_model=PaginatedSignalsOut)
def list_signals(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, max_length=200, description="Free-text search in title / body"),
    processed: bool | None = None,
    source_type: str | None = None,
    category: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    query = select(RawSignal).order_by(RawSignal.ingested_at.desc(), RawSignal.id.desc())
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(RawSignal.title.ilike(like), RawSignal.body.ilike(like)))
    if processed is not None:
        query = query.where(RawSignal.processed.is_(processed))
    if source_type:
        query = query.where(RawSignal.source_type == source_type)
    if category:
        query = query.where(RawSignal.normalized_category == category)

    total = db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    items = list(db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all())
    return PaginatedSignalsOut(total=total, page=page, page_size=page_size, items=items)


@router.get("/runs", response_model=PaginatedSourceRunsOut)
def list_runs(
    db: Session = Depends(get_db),
    source: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    query = select(SourceRun).order_by(SourceRun.started_at.desc(), SourceRun.id.desc())
    if source:
        query = query.where(SourceRun.source == source)
    if status_filter:
        query = query.where(SourceRun.status == status_filter)
    total = db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    items = list(db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all())
    return PaginatedSourceRunsOut(total=total, page=page, page_size=page_size, items=items)


@router.get("/{signal_id}", response_model=SignalOut)
def get_signal(signal_id: int, db: Session = Depends(get_db)):
    signal = db.execute(select(RawSignal).where(RawSignal.id == signal_id)).scalar_one_or_none()
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal


@router.post("/ingest", response_model=IngestOut)
def ingest_signals(payload: IngestRequest, db: Session = Depends(get_db)):
    run = create_source_run(db, source=payload.source, city=payload.city, query=payload.query)
    try:
        result = ingest_from_source(payload.source, payload.city, payload.query, payload.urls, geo=payload.geo)
    except ValueError as exc:
        finish_source_run(db, run, status="failed", fetched_count=0, created_signal_count=0, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Ingest failed for source=%s", payload.source)
        finish_source_run(db, run, status="failed", fetched_count=0, created_signal_count=0, error=str(exc))
        raise HTTPException(status_code=502, detail=f"Source fetch failed: {exc}") from exc

    created = []
    try:
        for item in result.items:
            created.append(create_signal(db, item))
    except Exception as exc:
        # The session may be in a failed state after a DB error — reset it
        # before recording the failure, otherwise finish_source_run fails too.
        db.rollback()
        logger.exception("Storing ingested signals failed for source=%s", payload.source)
        finish_source_run(
            db, run, status="failed", fetched_count=result.fetched_count,
            created_signal_count=len(created), error=str(exc),
        )
        raise HTTPException(status_code=500, detail="Failed to store ingested signals") from exc

    run_status = "completed" if not result.errors else ("partial" if created else "failed")
    finish_source_run(
        db, run, status=run_status, fetched_count=result.fetched_count,
        created_signal_count=len(created), error="; ".join(result.errors) or None,
    )
    return IngestOut(run_id=run.id, count=len(created), fetched_count=result.fetched_count, signal_ids=[s.id for s in created])


@router.post("/cluster", response_model=ClusterResponse)
def run_clustering(
    db: Session = Depends(get_db),
    limit: int | None = Query(default=None, ge=1, le=5000, description="Max signals to process in this pass"),
):
    created, linked, queued = cluster_signals(db, limit=limit)
    return ClusterResponse(
        created_event_ids=created,
        linked_signal_ids=linked,
        queued_review_ids=queued,
        remaining_unprocessed=count_unprocessed(db),
    )
