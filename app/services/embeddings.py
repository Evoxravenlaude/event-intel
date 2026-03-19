"""
Multilingual sentence embeddings service.

Uses `paraphrase-multilingual-MiniLM-L12-v2` from sentence-transformers by default.
This model is 50MB, CPU-friendly, and covers 100+ languages including English,
French, Yoruba-adjacent languages, Swahili, and Nigerian Pidgin.

Architecture
------------
- The model is loaded lazily on first use and cached as a module-level singleton.
- Embeddings are 384-dimensional float32 vectors.
- On PostgreSQL + pgvector: stored as native `vector(384)` columns and compared
  with the `<=>` cosine distance operator — fast nearest-neighbour at scale.
- On SQLite (dev/test): stored as JSON text and compared with Python cosine.

Storage
-------
Both `raw_signals` and `events` gain an `embedding` column (migration 0003).
The column is nullable — rows created before embeddings were enabled, or in
environments where sentence-transformers isn't installed, simply have NULL and
the system falls back to Jaccard bag-of-words similarity automatically.

Public API
----------
- encode(text)            -> list[float] | None
- cosine_similarity(a, b) -> float
- embed_signal(db, signal)  — writes embedding to signal row
- embed_event(db, event)    — writes embedding to event row
- nearest_events(db, signal, events) -> list[(Event, float)]
  Returns events sorted by cosine similarity, highest first.
"""
from __future__ import annotations
import logging
import math
from typing import TYPE_CHECKING

from sqlalchemy import select
from app.core.config import settings

if TYPE_CHECKING:
    from app.models.event import Event, RawSignal
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_model = None          # lazy singleton
_model_load_failed = False   # don't retry after ImportError / load failure


def _get_model():
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed or not settings.embeddings_enabled:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s", settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
        logger.info("Embedding model loaded.")
        return _model
    except Exception as exc:
        logger.warning("Failed to load embedding model (%s) — falling back to Jaccard: %s", settings.embedding_model, exc)
        _model_load_failed = True
        return None


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity — used when pgvector isn't available."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def encode(text: str | None) -> list[float] | None:
    """
    Encode text to a 384-dim embedding vector.
    Returns None if the model is unavailable or text is empty.
    """
    if not text or not text.strip():
        return None
    model = _get_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    except Exception as exc:
        logger.warning("Embedding encode failed: %s", exc)
        return None


def _serialise(vec: list[float] | None):
    """
    Return the value to assign to model.embedding.
    EmbeddingType handles JSON/vector conversion transparently,
    so we store the raw list directly.
    """
    return vec


def _deserialise(raw) -> list[float] | None:
    """
    EmbeddingType already returns list[float] | None when reading from DB.
    This helper handles legacy JSON strings that may exist in older rows
    (stored before EmbeddingType was introduced).
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    # Legacy: JSON string stored before TypeDecorator was added
    try:
        import json
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def embed_signal(db: "Session", signal: "RawSignal") -> None:
    """
    Compute and persist the embedding for a signal.
    Text used: title + body (the most informative fields).
    """
    text = " ".join(filter(None, [signal.title, signal.body]))
    vec = encode(text)
    if vec is not None:
        signal.embedding = _serialise(vec)
        db.add(signal)


def embed_event(db: "Session", event: "Event") -> None:
    """
    Compute and persist the embedding for an event.
    Text used: title + description.
    """
    text = " ".join(filter(None, [event.title, event.description]))
    vec = encode(text)
    if vec is not None:
        event.embedding = _serialise(vec)
        db.add(event)


# ---------------------------------------------------------------------------
# pgvector nearest-neighbour query
# ---------------------------------------------------------------------------

# How many candidate events to fetch from the HNSW index before applying
# the time/category/geo re-ranking boosts. 20 is generous for this use case.
PGVECTOR_CANDIDATE_K = 20


def nearest_events_pgvector(
    db: "Session",
    signal: "RawSignal",
    k: int = PGVECTOR_CANDIDATE_K,
) -> "list[Event] | None":
    """
    Return the k nearest events to `signal` using the pgvector HNSW index.

    Returns None when:
    - The signal has no embedding.
    - pgvector is not available on this database.
    - Any error occurs (caller falls back to full scan).

    The returned events are loaded with venue and evidence relationships
    eagerly so the caller can score them without extra queries.
    """
    from app.models.event import Event
    from app.services.geo import pgvector_available
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import selectinload

    sig_vec = _deserialise(signal.embedding)
    if not sig_vec:
        return None

    if not pgvector_available(db):
        return None

    try:
        # Use a properly parameterised binding via pgvector's SA integration.
        # This avoids string interpolation of the vector literal entirely.
        # pgvector registers the <=> operator on the Vector type; we use
        # sa_text with a named bindparam so the driver handles serialisation.
        from pgvector.sqlalchemy import Vector
        from sqlalchemy import cast, bindparam
        vec_param = cast(
            bindparam("sig_vec", value=sig_vec, type_=Vector(len(sig_vec))),
            Vector(len(sig_vec)),
        )
        stmt = (
            select(Event)
            .options(selectinload(Event.venue), selectinload(Event.evidence))
            .where(Event.embedding.is_not(None))
            .order_by(Event.embedding.op("<=>") (vec_param))
            .limit(k)
        )
        return list(db.execute(stmt).scalars().all())
    except Exception as exc:
        logger.warning("pgvector nearest-neighbour query failed, falling back to full scan: %s", exc)
        return None

def title_similarity(signal: "RawSignal", event: "Event") -> float:
    """
    Return a [0, 1] similarity score between a signal and an event.

    Priority:
    1. Cosine similarity of stored embeddings (multilingual, semantic).
    2. Jaccard bag-of-words fallback when either embedding is missing.
    """
    sig_vec = _deserialise(signal.embedding)
    evt_vec = _deserialise(event.embedding)

    if sig_vec and evt_vec:
        return cosine_similarity(sig_vec, evt_vec)

    # Jaccard fallback
    a = signal.title or ""
    b = event.title or ""
    if not a or not b:
        return 0.0
    a_set = set(a.lower().split())
    b_set = set(b.lower().split())
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)
