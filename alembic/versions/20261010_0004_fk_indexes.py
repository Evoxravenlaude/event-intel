"""Add indexes on foreign keys used by list/join queries

Revision ID: 20261010_0004
Revises: 20260317_0003
Create Date: 2026-10-10
"""
from alembic import op

revision = "20261010_0004"
down_revision = "20260317_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_events_venue_id", "events", ["venue_id"])
    op.create_index("ix_events_organizer_id", "events", ["organizer_id"])
    op.create_index("ix_review_queue_candidate_event_id", "review_queue", ["candidate_event_id"])
    op.create_index("ix_raw_signals_ingested_at", "raw_signals", ["ingested_at"])
    op.create_index("ix_review_queue_created_at", "review_queue", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_review_queue_created_at", table_name="review_queue")
    op.drop_index("ix_raw_signals_ingested_at", table_name="raw_signals")
    op.drop_index("ix_review_queue_candidate_event_id", table_name="review_queue")
    op.drop_index("ix_events_organizer_id", table_name="events")
    op.drop_index("ix_events_venue_id", table_name="events")
