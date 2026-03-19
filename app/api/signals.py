from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.event import RawSignal, SourceRun
from app.schemas.event import (
    ClusterResponse,
    IngestOut,
    IngestRequest,
    PaginatedSignalsOut,
    RawSignalCreate,
    SignalOut,
    SourceRunOut,
)
from app.services.adapters import ingest_from_source
from app.services.clustering import cluster_signals
from app.services.event_service import create_signal, create_source_run, finish_source_run

router = APIRouter(prefix="/signals", tags=["signals"])


@router.post("", response_model=SignalOut)
def post_signal(payload: RawSignalCreate, db: Session = Depends(get_db)):
    return create_signal(db, payload)


@router.get("", response_model=PaginatedSignalsOut)
def list_signals(
    db: Session = Depends(get_db),
    processed: bool | None = None,
    source_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    query = select(RawSignal).order_by(RawSignal.ingested_at.desc())
    if processed is not None:
        query = query.where(RawSignal.processed.is_(processed))
    if source_type:
        query = query.where(RawSignal.source_type == source_type)

    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    items = list(db.execute(query.offset((page - 1) * page_size).limit(page_size)).scalars().all())
    return PaginatedSignalsOut(total=total, page=page, page_size=page_size, items=items)


@router.get("/runs", response_model=list[SourceRunOut])
def list_runs(db: Session = Depends(get_db)):
    return db.execute(select(SourceRun).order_by(SourceRun.started_at.desc())).scalars().all()


@router.post("/ingest", response_model=IngestOut)
def ingest_signals(payload: IngestRequest, db: Session = Depends(get_db)):
    run = create_source_run(db, source=payload.source, city=payload.city, query=payload.query)
    try:
        result = ingest_from_source(payload.source, payload.city, payload.query, payload.urls)
        created = [create_signal(db, item) for item in result.items]
        finish_source_run(db, run, status="completed", fetched_count=result.fetched_count, created_signal_count=len(created))
        return IngestOut(run_id=run.id, count=len(created), signal_ids=[s.id for s in created])
    except Exception as exc:
        finish_source_run(db, run, status="failed", fetched_count=0, created_signal_count=0, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/cluster", response_model=ClusterResponse)
def run_clustering(db: Session = Depends(get_db)):
    created, linked, queued = cluster_signals(db)
    return ClusterResponse(created_event_ids=created, linked_signal_ids=linked, queued_review_ids=queued)
