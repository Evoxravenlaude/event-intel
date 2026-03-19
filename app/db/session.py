from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    # SQLite: single-threaded, no pool config needed
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
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
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
