"""
One-shot worker to back-fill embeddings on existing rows.

Run once after enabling embeddings on a database that already has signals
and events. Safe to run multiple times — skips rows that already have an
embedding.

Usage:
    python -m app.workers.backfill_embeddings

Environment variables:
    BACKFILL_BATCH_SIZE   rows to process per commit (default 100)
    EMBEDDINGS_ENABLED    must be true (default) for anything to happen

Progress is printed to stdout so Railway logs show it running. The worker
exits 0 on success and 1 on fatal error.
"""
from __future__ import annotations
import logging
import os
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.event import Event, RawSignal
from app.services import embeddings as emb_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("backfill_embeddings")

BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", 100))


def _backfill_table(
    db,
    model,
    text_fields: tuple[str, ...],
    label: str,
) -> int:
    """
    Back-fill embeddings for all rows of `model` where embedding IS NULL.

    Returns the total number of rows updated.
    """
    stmt = select(model).where(model.embedding.is_(None))
    rows = db.execute(stmt).scalars().all()
    total = len(rows)

    if total == 0:
        logger.info("%s: all rows already have embeddings — nothing to do", label)
        return 0

    logger.info("%s: %d rows need embeddings (batch size %d)", label, total, BATCH_SIZE)
    updated = 0

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        for row in batch:
            text = " ".join(str(getattr(row, f)) for f in text_fields if getattr(row, f))
            vec = emb_svc.encode(text)
            if vec is not None:
                row.embedding = vec
                db.add(row)
                updated += 1
        db.commit()
        logger.info(
            "%s: committed batch %d/%d (%d rows updated so far)",
            label,
            min(i + BATCH_SIZE, total),
            total,
            updated,
        )

    return updated


def main() -> None:
    from app.core.config import settings

    if not settings.embeddings_enabled:
        logger.warning(
            "EMBEDDINGS_ENABLED is false — backfill has nothing to do. "
            "Set EMBEDDINGS_ENABLED=true and re-run."
        )
        sys.exit(0)

    model = emb_svc._get_model()
    if model is None:
        logger.error(
            "Embedding model could not be loaded. "
            "Is sentence-transformers installed? Check logs above."
        )
        sys.exit(1)

    db = SessionLocal()
    try:
        signal_count = _backfill_table(db, RawSignal, ("title", "body"), "raw_signals")
        event_count  = _backfill_table(db, Event,     ("title", "description"), "events")
        logger.info(
            "Backfill complete — %d signal(s) and %d event(s) updated.",
            signal_count,
            event_count,
        )
    except Exception:
        logger.exception("Backfill failed with an unhandled exception")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
