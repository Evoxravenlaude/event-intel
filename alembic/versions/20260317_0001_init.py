"""init schema

Revision ID: 20260317_0001
Revises:
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa

revision = '20260317_0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'venues',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('city', sa.String(length=255), nullable=True),
        sa.Column('country', sa.String(length=255), nullable=True),
        sa.Column('address', sa.String(length=500), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
    )
    op.create_index('ix_venues_name', 'venues', ['name'])
    op.create_index('ix_venues_city', 'venues', ['city'])
    op.create_index('ix_venues_latitude', 'venues', ['latitude'])
    op.create_index('ix_venues_longitude', 'venues', ['longitude'])

    op.create_table(
        'organizers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('category_focus', sa.String(length=120), nullable=True),
        sa.Column('reliability_score', sa.Float(), nullable=False, server_default='0.5'),
    )
    op.create_index('ix_organizers_name', 'organizers', ['name'], unique=True)

    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=120), nullable=True),
        sa.Column('structure_type', sa.String(length=60), nullable=False, server_default='semi-structured'),
        sa.Column('status', sa.String(length=60), nullable=False, server_default='uncertain'),
        sa.Column('confidence_score', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('geo_precision_score', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('time_precision_score', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('start_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('venue_id', sa.Integer(), sa.ForeignKey('venues.id'), nullable=True),
        sa.Column('organizer_id', sa.Integer(), sa.ForeignKey('organizers.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_events_title', 'events', ['title'])
    op.create_index('ix_events_category', 'events', ['category'])
    op.create_index('ix_events_status', 'events', ['status'])
    op.create_index('ix_events_confidence_score', 'events', ['confidence_score'])
    op.create_index('ix_events_start_time', 'events', ['start_time'])

    op.create_table(
        'raw_signals',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source_type', sa.String(length=120), nullable=False),
        sa.Column('source_name', sa.String(length=120), nullable=True),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('location_text', sa.String(length=255), nullable=True),
        sa.Column('url', sa.String(length=1000), nullable=True),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('detected_start_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('detected_end_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('source_confidence', sa.Float(), nullable=False, server_default='0.4'),
        sa.Column('normalized_category', sa.String(length=120), nullable=True),
        sa.Column('processed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('source_type', 'external_id', name='uq_raw_signal_source_external'),
    )
    op.create_index('ix_raw_signals_source_type', 'raw_signals', ['source_type'])
    op.create_index('ix_raw_signals_processed', 'raw_signals', ['processed'])

    op.create_table(
        'event_evidence',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id'), nullable=False),
        sa.Column('raw_signal_id', sa.Integer(), sa.ForeignKey('raw_signals.id'), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False, server_default='0.2'),
        sa.Column('evidence_type', sa.String(length=120), nullable=False, server_default='mention'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_event_evidence_event_id', 'event_evidence', ['event_id'])
    op.create_index('ix_event_evidence_raw_signal_id', 'event_evidence', ['raw_signal_id'])

    op.create_table(
        'review_queue',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('raw_signal_id', sa.Integer(), sa.ForeignKey('raw_signals.id'), nullable=False),
        sa.Column('candidate_event_id', sa.Integer(), sa.ForeignKey('events.id'), nullable=True),
        sa.Column('reason', sa.String(length=255), nullable=False),
        sa.Column('score', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('status', sa.String(length=60), nullable=False, server_default='pending'),
        sa.Column('resolution_note', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_review_queue_status', 'review_queue', ['status'])
    op.create_index('ix_review_queue_raw_signal_id', 'review_queue', ['raw_signal_id'])

    op.create_table(
        'source_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source', sa.String(length=120), nullable=False),
        sa.Column('city', sa.String(length=255), nullable=True),
        sa.Column('query', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=60), nullable=False, server_default='started'),
        sa.Column('fetched_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_signal_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_source_runs_source', 'source_runs', ['source'])
    op.create_index('ix_source_runs_status', 'source_runs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_source_runs_status', table_name='source_runs')
    op.drop_index('ix_source_runs_source', table_name='source_runs')
    op.drop_table('source_runs')
    op.drop_index('ix_review_queue_raw_signal_id', table_name='review_queue')
    op.drop_index('ix_review_queue_status', table_name='review_queue')
    op.drop_table('review_queue')
    op.drop_index('ix_event_evidence_raw_signal_id', table_name='event_evidence')
    op.drop_index('ix_event_evidence_event_id', table_name='event_evidence')
    op.drop_table('event_evidence')
    op.drop_index('ix_raw_signals_processed', table_name='raw_signals')
    op.drop_index('ix_raw_signals_source_type', table_name='raw_signals')
    op.drop_table('raw_signals')
    op.drop_index('ix_events_start_time', table_name='events')
    op.drop_index('ix_events_confidence_score', table_name='events')
    op.drop_index('ix_events_status', table_name='events')
    op.drop_index('ix_events_category', table_name='events')
    op.drop_index('ix_events_title', table_name='events')
    op.drop_table('events')
    op.drop_index('ix_organizers_name', table_name='organizers')
    op.drop_table('organizers')
    op.drop_index('ix_venues_longitude', table_name='venues')
    op.drop_index('ix_venues_latitude', table_name='venues')
    op.drop_index('ix_venues_city', table_name='venues')
    op.drop_index('ix_venues_name', table_name='venues')
    op.drop_table('venues')
