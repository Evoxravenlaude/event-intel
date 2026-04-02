"""Add PostGIS location column to venues and venue_aliases table

Revision ID: 20260317_0002
Revises: 20260317_0001
Create Date: 2026-03-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "20260317_0002"
down_revision = "20260317_0001"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "venue_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue_id", sa.Integer(), sa.ForeignKey("venues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_venue_aliases_venue_id", "venue_aliases", ["venue_id"])
    op.create_index("ix_venue_aliases_alias", "venue_aliases", ["alias"])

    if not _is_postgres():
        return

    # PostGIS — use a SAVEPOINT so a failure here doesn't abort the whole
    # transaction. Without this, a failed CREATE EXTENSION puts Postgres into
    # an aborted-transaction state and every subsequent statement (including
    # Alembic writing to alembic_version) fails too.
    bind = op.get_bind()
    bind.execute(text("SAVEPOINT sp_postgis"))
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        op.execute("ALTER TABLE venues ADD COLUMN IF NOT EXISTS location geography(Point, 4326)")
        op.execute(
            "UPDATE venues SET location = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
        )
        op.execute("CREATE INDEX IF NOT EXISTS ix_venues_location ON venues USING GIST (location)")
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
        # Split into two separate executes — psycopg3 does not allow multiple
        # statements in a single execute() call.
        op.execute("DROP TRIGGER IF EXISTS trg_sync_venue_location ON venues")
        op.execute("""
            CREATE TRIGGER trg_sync_venue_location
            BEFORE INSERT OR UPDATE OF latitude, longitude ON venues
            FOR EACH ROW EXECUTE FUNCTION sync_venue_location()
        """)
        bind.execute(text("RELEASE SAVEPOINT sp_postgis"))
    except Exception:
        # Roll back only the PostGIS sub-block; the rest of the transaction
        # (venue_aliases table + indexes) remains intact and can commit.
        bind.execute(text("ROLLBACK TO SAVEPOINT sp_postgis"))
        # PostGIS not available — radius queries will use Haversine fallback


def downgrade() -> None:
    if _is_postgres():
        bind = op.get_bind()
        bind.execute(text("SAVEPOINT sp_postgis_down"))
        try:
            op.execute("DROP TRIGGER IF EXISTS trg_sync_venue_location ON venues")
            op.execute("DROP FUNCTION IF EXISTS sync_venue_location()")
            op.execute("DROP INDEX IF EXISTS ix_venues_location")
            op.execute("ALTER TABLE venues DROP COLUMN IF EXISTS location")
            bind.execute(text("RELEASE SAVEPOINT sp_postgis_down"))
        except Exception:
            bind.execute(text("ROLLBACK TO SAVEPOINT sp_postgis_down"))

    op.drop_index("ix_venue_aliases_alias", table_name="venue_aliases")
    op.drop_index("ix_venue_aliases_venue_id", table_name="venue_aliases")
    op.drop_table("venue_aliases")
