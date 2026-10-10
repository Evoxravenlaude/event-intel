"""
pytest configuration and fixtures.

Each test function gets a fresh in-memory SQLite database and a TestClient
wired to it. Nothing bleeds between tests.
"""
from __future__ import annotations
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Point at in-memory SQLite before any app code is imported
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_MOCK_ADAPTERS", "true")
os.environ.setdefault("EMBEDDINGS_ENABLED", "false")   # no model downloads in tests
os.environ.setdefault("API_KEY", "")
os.environ.setdefault("WEBHOOK_URLS", "")
os.environ.setdefault("SCHEDULER_SOURCES", "")
os.environ.setdefault("GEOCODING_ENABLED", "false")   # offline gazetteer only — no network in tests


@pytest.fixture()
def db_engine():
    """
    Create a fresh in-memory SQLite engine for each test.

    StaticPool is essential: an in-memory SQLite database exists per
    connection, and TestClient drives the app from a different thread than
    the test. Without StaticPool that thread got a brand-new empty database
    and every request failed with "no such table".
    """
    from app.db.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @sa_event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Provide a session scoped to a single test."""
    SessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def client(db_engine):
    """
    Return a TestClient whose database dependency is overridden to use the
    per-test in-memory engine. Each call to get_db yields a fresh session
    from that engine.
    """
    from app.main import app
    from app.db.session import get_db
    import app.services.geo as geo_mod

    TestingSessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False, future=True)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    geo_mod.reset_capability_cache()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_client(client, monkeypatch):
    """Client with API key enforcement switched on."""
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "test-secret-key")
    client.headers.update({"X-API-Key": "test-secret-key"})
    return client
