"""
Custom SQLAlchemy column types.

UTCDateTime
    A timezone-aware DateTime that is *guaranteed* to come back from the
    database as an aware UTC datetime on every backend.

    PostgreSQL `timestamptz` already does this. SQLite has no timezone
    support: SQLAlchemy stores the ISO string and hands back a *naive*
    datetime, which then blows up the moment it is compared with
    `datetime.now(timezone.utc)` ("can't compare offset-naive and
    offset-aware datetimes"). That crash took down clustering in local dev.

    On write, naive datetimes are assumed to be UTC. On read, naive values
    are re-tagged as UTC. Aware values are normalised to UTC in both
    directions so comparisons are consistent regardless of what the client sent.

EmbeddingType
    Stores 384-dim float vectors in a way that works on both backends:

    - PostgreSQL + pgvector: native `vector(384)` column.
      Reads and writes Python list[float] directly.
      Enables HNSW nearest-neighbour index and <=> cosine operator.

    - SQLite (dev / test): TEXT column, JSON-serialised.
      Python cosine is used for comparison in embeddings.py.

    The TypeDecorator pattern means model code never needs to know which
    backend is active — it always reads and writes list[float] | None.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from sqlalchemy import DateTime, Text
from sqlalchemy.types import TypeDecorator

VECTOR_DIM = 384


def to_utc(value: datetime | None) -> datetime | None:
    """Return `value` as an aware UTC datetime (naive input is assumed UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware DateTime that always round-trips as aware UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return to_utc(value)

    def process_result_value(self, value, dialect):
        return to_utc(value)


class EmbeddingType(TypeDecorator):
    """
    Database-agnostic embedding column.

    Value in Python: list[float] | None
    Value in PostgreSQL: vector(384)   — requires pgvector extension
    Value in SQLite:     TEXT          — JSON-serialised
    """
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector
                return dialect.type_descriptor(Vector(VECTOR_DIM))
            except ImportError:
                pass  # pgvector not installed; fall through to Text
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        """Python → DB."""
        if value is None:
            return None
        if dialect.name == "postgresql":
            # pgvector accepts a Python list directly
            return value
        # SQLite: serialise to JSON
        return json.dumps(value) if isinstance(value, list) else value

    def process_result_value(self, value, dialect):
        """DB → Python."""
        if value is None:
            return None
        if isinstance(value, list):
            # pgvector already deserialised to a list
            return value
        if hasattr(value, "tolist"):
            # pgvector may hand back a numpy array
            return value.tolist()
        # SQLite JSON text
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
