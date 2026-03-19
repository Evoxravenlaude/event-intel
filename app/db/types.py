"""
Custom SQLAlchemy column types.

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

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator, UserDefinedType

VECTOR_DIM = 384


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
        # SQLite JSON text
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
