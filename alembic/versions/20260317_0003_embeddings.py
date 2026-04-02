"""Add embedding columns and pgvector index

Revision ID: 20260317_0003
Revises: 20260317_0002
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa

revision = "20260317_0003"
down_revision = "20260317_0002"
branch_labels = None
depends_on = None

VECTOR_DIM = 384


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        # pgvector — skip silently if extension not available
        try:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            op.execute(f"ALTER TABLE events ADD COLUMN IF NOT EXISTS embedding vector({VECTOR_DIM})")
            op.execute(f"ALTER TABLE raw_signals ADD COLUMN IF NOT EXISTS embedding vector({VECTOR_DIM})")
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_events_embedding_hnsw "
                f"ON events USING hnsw (embedding vector_cosine_ops)"
            )
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_raw_signals_embedding_hnsw "
                f"ON raw_signals USING hnsw (embedding vector_cosine_ops)"
            )
        except Exception:
            # pgvector not available — fall back to TEXT columns
            op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS embedding TEXT")
            op.execute("ALTER TABLE raw_signals ADD COLUMN IF NOT EXISTS embedding TEXT")
    else:
        op.add_column("events", sa.Column("embedding", sa.Text(), nullable=True))
        op.add_column("raw_signals", sa.Column("embedding", sa.Text(), nullable=True))


def downgrade() -> None:
    if _is_postgres():
        try:
            op.execute("DROP INDEX IF EXISTS ix_raw_signals_embedding_hnsw")
            op.execute("DROP INDEX IF EXISTS ix_events_embedding_hnsw")
            op.execute("ALTER TABLE raw_signals DROP COLUMN IF EXISTS embedding")
            op.execute("ALTER TABLE events DROP COLUMN IF EXISTS embedding")
        except Exception:
            pass
    else:
        op.drop_column("raw_signals", "embedding")
        op.drop_column("events", "embedding")
