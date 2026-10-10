from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings

if settings.is_sqlite:
    is_memory = ":memory:" in settings.database_url or settings.database_url.rstrip("/") == "sqlite://"
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        # An in-memory SQLite database lives *per connection*. Without a
        # StaticPool every pooled connection sees its own empty database and
        # queries fail with "no such table". File-backed SQLite uses the default pool.
        poolclass=StaticPool if is_memory else None,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # Enforce FKs (off by default) and reduce writer lock contention
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not is_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
else:
    # PostgreSQL: tune pool for a typical single-instance Railway deployment.
    # pool_size=5, max_overflow=10 → up to 15 connections under burst load.
    # pool_pre_ping checks connections before use, avoiding stale connection errors
    # after Supabase's idle connection timeout.
    engine = create_engine(
        settings.database_url,
        future=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
