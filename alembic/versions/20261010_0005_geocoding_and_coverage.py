"""Geocode cache, coverage cells, venue country_code/geo_source

Revision ID: 20261010_0005
Revises: 20261010_0004
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa

revision = "20261010_0005"
down_revision = "20261010_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("venues") as batch:
        batch.add_column(sa.Column("country_code", sa.String(2), nullable=True))
        batch.add_column(sa.Column("geo_source", sa.String(20), nullable=True))
    op.create_index("ix_venues_country_code", "venues", ["country_code"])

    op.create_table(
        "geocode_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("query", sa.String(500), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_geocode_cache_key", "geocode_cache", ["key"], unique=True)

    op.create_table(
        "coverage_cells",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cell", sa.String(12), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("radius_km", sa.Float(), nullable=False, server_default="25"),
        sa.Column("city", sa.String(255), nullable=True),
        sa.Column("country_code", sa.String(2), nullable=True),
        sa.Column("scan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_signal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_coverage_cells_cell", "coverage_cells", ["cell"], unique=True)
    op.create_index("ix_coverage_cells_latitude", "coverage_cells", ["latitude"])
    op.create_index("ix_coverage_cells_longitude", "coverage_cells", ["longitude"])
    op.create_index("ix_coverage_cells_city", "coverage_cells", ["city"])
    op.create_index("ix_coverage_cells_last_scanned_at", "coverage_cells", ["last_scanned_at"])


def downgrade() -> None:
    op.drop_index("ix_coverage_cells_last_scanned_at", table_name="coverage_cells")
    op.drop_index("ix_coverage_cells_city", table_name="coverage_cells")
    op.drop_index("ix_coverage_cells_longitude", table_name="coverage_cells")
    op.drop_index("ix_coverage_cells_latitude", table_name="coverage_cells")
    op.drop_index("ix_coverage_cells_cell", table_name="coverage_cells")
    op.drop_table("coverage_cells")
    op.drop_index("ix_geocode_cache_key", table_name="geocode_cache")
    op.drop_table("geocode_cache")
    op.drop_index("ix_venues_country_code", table_name="venues")
    with op.batch_alter_table("venues") as batch:
        batch.drop_column("geo_source")
        batch.drop_column("country_code")
