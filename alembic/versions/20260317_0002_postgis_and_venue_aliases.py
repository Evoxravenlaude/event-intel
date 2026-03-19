"""Add PostGIS location column to venues and venue_aliases table

Revision ID: 20260317_0002
Revises: 20260317_0001
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa

revision = "20260317_0002"
down_revision = "20260317_0001"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # venue_aliases — stores known alternative names for a canonical venue
    # ------------------------------------------------------------------
    op.create_table(
        "venue_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue_id", sa.Integer(), sa.ForeignKey("venues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),   # where the alias was seen
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_venue_aliases_venue_id", "venue_aliases", ["venue_id"])
    op.create_index("ix_venue_aliases_alias", "venue_aliases", ["alias"])

    if not _is_postgres():
        return  # SQLite — skip PostGIS-specific DDL

    # ------------------------------------------------------------------
    # PostGIS: add geography column to venues and build a spatial index
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # Add a computed geography point from lat/lng
    op.execute(
        "ALTER TABLE venues "
        "ADD COLUMN IF NOT EXISTS location geography(Point, 4326)"
    )

    # Back-fill from existing lat/lng data
    op.execute(
        "UPDATE venues "
        "SET location = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    )

    # Spatial index — used by ST_DWithin radius queries
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_venues_location "
        "ON venues USING GIST (location)"
    )

    # Trigger to keep location in sync whenever lat/lng are updated
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_venue_location()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.latitude IS NOT NULL AND NEW.longitude IS NOT NULL THEN
                NEW.location := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326)::geography;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS trg_sync_venue_location ON venues;
        CREATE TRIGGER trg_sync_venue_location
        BEFORE INSERT OR UPDATE OF latitude, longitude ON venues
        FOR EACH ROW EXECUTE FUNCTION sync_venue_location();
    """)


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP TRIGGER IF EXISTS trg_sync_venue_location ON venues")
        op.execute("DROP FUNCTION IF EXISTS sync_venue_location()")
        op.execute("DROP INDEX IF EXISTS ix_venues_location")
        op.execute("ALTER TABLE venues DROP COLUMN IF EXISTS location")

    op.drop_index("ix_venue_aliases_alias", table_name="venue_aliases")
    op.drop_index("ix_venue_aliases_venue_id", table_name="venue_aliases")
    op.drop_table("venue_aliases")
