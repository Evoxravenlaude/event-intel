"""venues.geo_accuracy + venues.neighbourhood

Revision ID: 20261012_0006
Revises: 20261010_0005
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = "20261012_0006"
down_revision = "20261010_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("venues") as batch:
        batch.add_column(sa.Column("geo_accuracy", sa.String(10), nullable=True))
        batch.add_column(sa.Column("neighbourhood", sa.String(255), nullable=True))
    # Existing rows: source coordinates are exact, the rest were city-level guesses at best.
    op.execute("UPDATE venues SET geo_accuracy = 'exact' WHERE geo_source = 'source'")
    op.execute("UPDATE venues SET geo_accuracy = 'city' WHERE geo_source = 'gazetteer'")
    op.execute("UPDATE venues SET geo_accuracy = 'area' WHERE geo_accuracy IS NULL AND latitude IS NOT NULL")
    # The old reverse geocoder labelled Lagos venues with LGA names ("Eti Osa"); clear
    # source-located cities so the new metro-aware geocoder relabels them on next touch.
    op.execute("DELETE FROM geocode_cache")


def downgrade() -> None:
    with op.batch_alter_table("venues") as batch:
        batch.drop_column("neighbourhood")
        batch.drop_column("geo_accuracy")
