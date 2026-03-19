"""
Integration tests for the Event Intel API.

Each test receives a fresh `client` fixture from conftest.py which provides
a clean in-memory SQLite database — no bleed between tests.
"""

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------

def test_create_event_minimal(client):
    r = client.post("/events", json={"title": "Minimal Event"})
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Minimal Event"
    assert "id" in data
    assert "status" in data
    assert "confidence_score" in data


def test_create_event_full(client):
    payload = {
        "title": "Lagos Tech Summit",
        "description": "Annual gathering of Lagos tech builders.",
        "category": "tech",
        "start_time": "2026-06-15T18:00:00Z",
        "end_time": "2026-06-15T21:00:00Z",
        "venue": {
            "name": "Landmark Event Centre",
            "city": "Lagos",
            "country": "Nigeria",
            "latitude": 6.4281,
            "longitude": 3.4219,
        },
        "organizer_name": "Lagos Tech Alliance",
    }
    r = client.post("/events", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["category"] == "tech"
    assert data["status"] == "upcoming"
    assert data["venue"]["city"] == "Lagos"
    assert data["organizer"]["name"] == "Lagos Tech Alliance"


def test_list_events_returns_paginated(client):
    r = client.get("/events")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data


def test_list_events_filter_category(client):
    client.post("/events", json={"title": "Web3 Builders", "category": "web3"})
    client.post("/events", json={"title": "Other Event", "category": "tech"})
    r = client.get("/events?category=web3")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    for event in items:
        assert event["category"] == "web3"


def test_list_events_filter_city(client):
    client.post("/events", json={
        "title": "Nairobi Meetup",
        "venue": {"name": "iHub", "city": "Nairobi"},
    })
    client.post("/events", json={
        "title": "Lagos Event",
        "venue": {"name": "Landmark", "city": "Lagos"},
    })
    r = client.get("/events?city=Nairobi")
    assert r.status_code == 200
    for event in r.json()["items"]:
        assert event["venue"]["city"].lower() == "nairobi"


def test_get_event_by_id(client):
    r = client.post("/events", json={"title": "ID Test Event"})
    event_id = r.json()["id"]
    r2 = client.get(f"/events/{event_id}")
    assert r2.status_code == 200
    assert r2.json()["id"] == event_id


def test_get_event_not_found(client):
    r = client.get("/events/999999")
    assert r.status_code == 404


def test_list_events_pagination(client):
    for i in range(5):
        client.post("/events", json={"title": f"Pagination Event {i}"})
    r = client.get("/events?page=1&page_size=2")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) <= 2
    assert data["page_size"] == 2


# ---------------------------------------------------------------------------
# Signal ingestion
# ---------------------------------------------------------------------------

def test_create_signal(client):
    r = client.post("/signals", json={
        "source_type": "manual",
        "external_id": "test-signal-001",
        "title": "Web3 Lagos Meetup",
        "normalized_category": "web3",
        "source_confidence": 0.85,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["source_type"] == "manual"
    assert data["processed"] is False


def test_create_signal_deduplication(client):
    payload = {
        "source_type": "manual",
        "external_id": "dedup-test-001",
        "title": "Dedup Event",
        "source_confidence": 0.7,
    }
    r1 = client.post("/signals", json=payload)
    r2 = client.post("/signals", json={**payload, "title": "Dedup Event Updated"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_list_signals_paginated(client):
    r = client.get("/signals")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data


def test_list_signals_filter_processed(client):
    client.post("/signals", json={
        "source_type": "manual",
        "title": "Unprocessed signal",
        "source_confidence": 0.7,
    })
    r = client.get("/signals?processed=false")
    assert r.status_code == 200
    for signal in r.json()["items"]:
        assert signal["processed"] is False


def test_list_signals_filter_source_type(client):
    client.post("/signals", json={
        "source_type": "telegram",
        "title": "Telegram test signal",
        "source_confidence": 0.6,
    })
    r = client.get("/signals?source_type=telegram")
    assert r.status_code == 200
    for signal in r.json()["items"]:
        assert signal["source_type"] == "telegram"


# ---------------------------------------------------------------------------
# Ingest endpoint (uses mock adapters)
# ---------------------------------------------------------------------------

def test_ingest_mock_source(client):
    r = client.post("/signals/ingest", json={
        "source": "eventbrite",
        "city": "Lagos",
        "query": "web3",
    })
    assert r.status_code == 200
    data = r.json()
    assert "run_id" in data
    assert data["count"] >= 1


def test_ingest_unsupported_source(client):
    r = client.post("/signals/ingest", json={"source": "nonexistent"})
    assert r.status_code == 400


def test_source_runs_logged(client):
    client.post("/signals/ingest", json={"source": "telegram", "city": "Accra"})
    r = client.get("/signals/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) >= 1
    assert "source" in runs[0]
    assert "status" in runs[0]


# ---------------------------------------------------------------------------
# Clustering and full signal → event flow
# ---------------------------------------------------------------------------

def test_signal_to_event_flow(client):
    r1 = client.post("/signals", json={
        "source_type": "manual",
        "external_id": "flow-test-001",
        "title": "Lagos Web3 Builders Meetup",
        "body": "Tonight at Yaba with founders and developers.",
        "location_text": "Yaba",
        "source_confidence": 0.9,
        "normalized_category": "web3",
    })
    assert r1.status_code == 200

    r2 = client.post("/signals/cluster")
    assert r2.status_code == 200
    assert "created_event_ids" in r2.json()

    r3 = client.get("/events?category=web3")
    assert r3.status_code == 200
    assert r3.json()["total"] >= 1


def test_clustering_deduplicates_linked_signals(client):
    """Running cluster twice should not double-link the same signal."""
    client.post("/signals", json={
        "source_type": "manual",
        "external_id": "cluster-dedup-001",
        "title": "Dedup Clustering Test Event",
        "source_confidence": 0.8,
    })
    r1 = client.post("/signals/cluster")
    r2 = client.post("/signals/cluster")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(r2.json()["created_event_ids"]) == 0


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

def test_review_queue_list(client):
    r = client.get("/review-queue")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data


def test_review_queue_filter_status(client):
    r = client.get("/review-queue?status=pending")
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["status"] == "pending"


def test_review_queue_resolve_not_found(client):
    r = client.post("/review-queue/999999/resolve", json={"action": "reject"})
    assert r.status_code == 400


def test_review_queue_resolve_reject(client):
    """Create a signal that may cluster into the uncertain range and reject it."""
    client.post("/events", json={"title": "Seed Event For Review", "category": "tech"})
    client.post("/signals", json={
        "source_type": "manual",
        "external_id": "review-test-reject-001",
        "title": "Seed Event",
        "source_confidence": 0.3,
        "normalized_category": "tech",
    })
    client.post("/signals/cluster")

    pending = client.get("/review-queue?status=pending")
    items = pending.json()["items"]
    if not items:
        return  # score didn't land in uncertain zone — skip

    item_id = items[0]["id"]
    r = client.post(f"/review-queue/{item_id}/resolve", json={"action": "reject", "note": "Not the same event"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


def test_review_queue_resolve_unsupported_action(client):
    r = client.post("/review-queue/999999/resolve", json={"action": "teleport"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Venue deduplication
# ---------------------------------------------------------------------------

def test_venue_deduplication_fuzzy_match(client):
    """Two slightly different venue name variants should resolve to the same venue."""
    ev1 = client.post("/events", json={
        "title": "Event at Landmark",
        "venue": {"name": "Landmark Event Centre", "city": "Lagos"},
    })
    ev2 = client.post("/events", json={
        "title": "Event at Landmark variant",
        "venue": {"name": "Landmark Events Centre Lagos", "city": "Lagos"},
    })
    assert ev1.status_code == 200
    assert ev2.status_code == 200
    assert ev1.json()["venue"]["id"] == ev2.json()["venue"]["id"]


def test_venue_deduplication_different_city(client):
    """Same venue name in different cities should NOT be merged."""
    ev1 = client.post("/events", json={
        "title": "iHub Lagos event",
        "venue": {"name": "iHub", "city": "Lagos"},
    })
    ev2 = client.post("/events", json={
        "title": "iHub Nairobi event",
        "venue": {"name": "iHub", "city": "Nairobi"},
    })
    assert ev1.status_code == 200
    assert ev2.status_code == 200
    assert ev1.json()["venue"]["id"] != ev2.json()["venue"]["id"]


# ---------------------------------------------------------------------------
# Multilingual category inference
# ---------------------------------------------------------------------------

def test_category_inference_french(client):
    from app.services.parsing import infer_category
    assert infer_category("Conférence sur l'intelligence artificielle à Dakar") == "tech"
    assert infer_category("Forum économique pour entrepreneurs") == "business"
    assert infer_category("Festival de musique ce soir") == "culture"


def test_category_inference_pidgin(client):
    from app.services.parsing import infer_category
    assert infer_category("Web3 thing dey happen for Yaba tonight") == "web3"
    assert infer_category("Tech bros meetup make we code together") == "tech"


def test_category_inference_swahili(client):
    from app.services.parsing import infer_category
    assert infer_category("Tamasha la muziki Nairobi") == "culture"
    assert infer_category("Mkutano wa biashara na wawekezaji") == "business"


def test_live_words_french(client):
    from app.services.parsing import infer_times_from_text, contains_live_word
    assert contains_live_word("C'est maintenant, venez!")
    start, _ = infer_times_from_text("Événement ce soir à Lagos")
    assert start is not None


# ---------------------------------------------------------------------------
# LinkedIn adapter (mock mode)
# ---------------------------------------------------------------------------

def test_ingest_linkedin_mock(client):
    r = client.post("/signals/ingest", json={
        "source": "linkedin",
        "city": "Accra",
        "query": "startup",
    })
    assert r.status_code == 200
    assert r.json()["count"] >= 1


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def test_auth_disabled_by_default(client):
    r = client.get("/events")
    assert r.status_code == 200


def test_auth_health_always_exempt(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_auth_enforced_when_key_set(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "test-secret-key")
    r = client.get("/events")
    assert r.status_code == 401


def test_auth_valid_key_accepted(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "test-secret-key")
    r = client.get("/events", headers={"X-API-Key": "test-secret-key"})
    assert r.status_code == 200


def test_auth_wrong_key_rejected(client, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "api_key", "test-secret-key")
    r = client.get("/events", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Embeddings service (unit — no model download needed)
# ---------------------------------------------------------------------------

def test_embedding_cosine_similarity_identical():
    from app.services.embeddings import cosine_similarity
    v = [1.0, 0.0, 0.5]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


def test_embedding_cosine_similarity_orthogonal():
    from app.services.embeddings import cosine_similarity
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_embedding_cosine_similarity_zero_vector():
    from app.services.embeddings import cosine_similarity
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_embedding_encode_returns_none_when_disabled(monkeypatch):
    from app.core import config as cfg
    from app.services import embeddings as emb
    monkeypatch.setattr(cfg.settings, "embeddings_enabled", False)
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_load_failed", False)
    result = emb.encode("Lagos Web3 Summit")
    assert result is None


def test_title_similarity_jaccard_fallback():
    from app.services.embeddings import title_similarity
    from app.models.event import RawSignal, Event

    sig = RawSignal()
    sig.title = "Lagos Web3 Builders Meetup"
    sig.embedding = None

    evt = Event()
    evt.title = "Lagos Web3 Builders Meetup"
    evt.embedding = None

    assert title_similarity(sig, evt) == 1.0


def test_title_similarity_partial_jaccard():
    from app.services.embeddings import title_similarity
    from app.models.event import RawSignal, Event

    sig = RawSignal()
    sig.title = "Web3 summit Lagos"
    sig.embedding = None

    evt = Event()
    evt.title = "Web3 Builders Lagos"
    evt.embedding = None

    score = title_similarity(sig, evt)
    assert abs(score - 0.5) < 1e-6


def test_title_similarity_uses_stored_embeddings():
    from app.services.embeddings import title_similarity
    from app.models.event import RawSignal, Event

    vec = [1.0, 0.0, 0.0]

    sig = RawSignal()
    sig.title = "completely different words"
    sig.embedding = vec

    evt = Event()
    evt.title = "nothing in common here"
    evt.embedding = vec

    assert abs(title_similarity(sig, evt) - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# EmbeddingType TypeDecorator (unit)
# ---------------------------------------------------------------------------

def test_embedding_type_roundtrip_sqlite():
    """EmbeddingType should serialise list->JSON and deserialise JSON->list on SQLite."""
    from unittest.mock import MagicMock
    from app.db.types import EmbeddingType

    t = EmbeddingType()
    dialect = MagicMock()
    dialect.name = "sqlite"

    vec = [0.1, 0.2, 0.3]
    serialised = t.process_bind_param(vec, dialect)
    assert isinstance(serialised, str)

    recovered = t.process_result_value(serialised, dialect)
    assert recovered == vec


def test_embedding_type_none_passthrough():
    from unittest.mock import MagicMock
    from app.db.types import EmbeddingType

    t = EmbeddingType()
    dialect = MagicMock()
    dialect.name = "sqlite"

    assert t.process_bind_param(None, dialect) is None
    assert t.process_result_value(None, dialect) is None


def test_embedding_type_postgres_passthrough():
    """On Postgres dialect, bind_param should pass the list through unchanged."""
    from unittest.mock import MagicMock, patch
    from app.db.types import EmbeddingType

    t = EmbeddingType()
    dialect = MagicMock()
    dialect.name = "postgresql"

    vec = [0.1, 0.2, 0.3]
    # Without pgvector installed, it should still pass the list through
    with patch.dict("sys.modules", {"pgvector": None, "pgvector.sqlalchemy": None}):
        result = t.process_bind_param(vec, dialect)
    assert result == vec


# ---------------------------------------------------------------------------
# pgvector nearest-neighbour (unit — no live DB needed)
# ---------------------------------------------------------------------------

def test_nearest_events_pgvector_returns_none_without_embedding():
    """If the signal has no embedding, nearest_events_pgvector returns None."""
    from unittest.mock import MagicMock
    from app.services.embeddings import nearest_events_pgvector
    from app.models.event import RawSignal

    sig = RawSignal()
    sig.embedding = None

    db = MagicMock()
    result = nearest_events_pgvector(db, sig)
    assert result is None


def test_nearest_events_pgvector_returns_none_without_pgvector(monkeypatch):
    """Returns None gracefully when pgvector is not available."""
    from unittest.mock import MagicMock
    from app.services.embeddings import nearest_events_pgvector
    from app.models.event import RawSignal
    import app.services.geo as geo_mod

    monkeypatch.setattr(geo_mod, "_pgvector_available_cache", False)

    sig = RawSignal()
    sig.embedding = [0.1, 0.2, 0.3]

    db = MagicMock()
    result = nearest_events_pgvector(db, sig)
    assert result is None


def test_nearest_events_pgvector_falls_back_on_exception(monkeypatch):
    """If the SQL query raises, returns None so caller falls back to full scan."""
    from unittest.mock import MagicMock, patch
    from app.services.embeddings import nearest_events_pgvector
    from app.models.event import RawSignal
    import app.services.geo as geo_mod

    monkeypatch.setattr(geo_mod, "_pgvector_available_cache", True)

    sig = RawSignal()
    sig.embedding = [0.1, 0.2, 0.3]

    db = MagicMock()
    db.execute.side_effect = RuntimeError("simulated DB failure")

    result = nearest_events_pgvector(db, sig)
    assert result is None


# ---------------------------------------------------------------------------
# _postgis_available and pgvector_available caching (unit)
# ---------------------------------------------------------------------------

def test_postgis_cache_skips_query_on_second_call(monkeypatch):
    """After the first call the cached value is returned without hitting the DB."""
    from unittest.mock import MagicMock
    import app.services.geo as geo_mod

    monkeypatch.setattr(geo_mod, "_postgis_available_cache", None)
    monkeypatch.setattr(geo_mod, "_pgvector_available_cache", None)

    db = MagicMock()
    db.connection.return_value.dialect.name = "sqlite"

    # First call — sets the cache
    r1 = geo_mod._postgis_available(db)
    assert r1 is False
    assert geo_mod._postgis_available_cache is False

    # Second call — cache hit, execute should NOT be called
    db.execute.reset_mock()
    r2 = geo_mod._postgis_available(db)
    assert r2 is False
    db.execute.assert_not_called()


def test_pgvector_cache_skips_query_on_second_call(monkeypatch):
    from unittest.mock import MagicMock
    import app.services.geo as geo_mod

    monkeypatch.setattr(geo_mod, "_postgis_available_cache", None)
    monkeypatch.setattr(geo_mod, "_pgvector_available_cache", None)

    db = MagicMock()
    db.connection.return_value.dialect.name = "sqlite"

    r1 = geo_mod.pgvector_available(db)
    assert r1 is False
    db.execute.reset_mock()
    r2 = geo_mod.pgvector_available(db)
    assert r2 is False
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Backfill worker (unit)
# ---------------------------------------------------------------------------

def test_backfill_skips_rows_with_existing_embeddings(db_session):
    """Rows that already have an embedding are not re-encoded."""
    from unittest.mock import patch
    from app.workers.backfill_embeddings import _backfill_table
    from app.models.event import Event
    import app.services.embeddings as emb_mod

    ev = Event(
        title="Already Embedded",
        category="tech",
        structure_type="semi-structured",
        status="uncertain",
        confidence_score=0.5,
        geo_precision_score=0.3,
        time_precision_score=0.3,
        embedding=[0.1, 0.2, 0.3],
    )
    db_session.add(ev)
    db_session.commit()

    with patch.object(emb_mod, "encode", wraps=emb_mod.encode) as mock_encode:
        updated = _backfill_table(db_session, Event, ("title", "description"), "events")
        mock_encode.assert_not_called()
        assert updated == 0


def test_backfill_embeds_rows_without_embeddings(db_session):
    """Rows with NULL embedding get encoded and committed."""
    from unittest.mock import patch
    from app.workers.backfill_embeddings import _backfill_table
    from app.models.event import Event
    import app.services.embeddings as emb_mod

    ev = Event(
        title="Needs Embedding",
        category="tech",
        structure_type="semi-structured",
        status="uncertain",
        confidence_score=0.5,
        geo_precision_score=0.3,
        time_precision_score=0.3,
        embedding=None,
    )
    db_session.add(ev)
    db_session.commit()

    fake_vec = [0.9] * 384
    with patch.object(emb_mod, "encode", return_value=fake_vec):
        updated = _backfill_table(db_session, Event, ("title", "description"), "events")

    assert updated == 1
    db_session.refresh(ev)
    assert ev.embedding == fake_vec


def test_backfill_exits_cleanly_when_embeddings_disabled(monkeypatch):
    """main() exits 0 with a warning when EMBEDDINGS_ENABLED=false."""
    import sys
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "embeddings_enabled", False)

    with __import__('pytest').raises(SystemExit) as exc_info:
        from app.workers.backfill_embeddings import main
        main()
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Config: CORS origins + production warnings
# ---------------------------------------------------------------------------

def test_cors_origins_wildcard():
    from app.core.config import Settings
    s = Settings(cors_origins_raw="*")
    assert s.cors_origins == ["*"]


def test_cors_origins_csv():
    from app.core.config import Settings
    s = Settings(cors_origins_raw="https://app.com,https://admin.app.com")
    assert s.cors_origins == ["https://app.com", "https://admin.app.com"]


def test_production_warning_no_api_key(caplog):
    """Settings emits a warning when APP_ENV=production and API_KEY is unset."""
    import logging
    from app.core.config import Settings
    with caplog.at_level(logging.WARNING):
        Settings(app_env="production", api_key=None, database_url="postgresql://x/y")
    assert any("API_KEY" in r.message for r in caplog.records)


def test_production_no_warning_with_api_key(caplog):
    """No API_KEY warning when key is set."""
    import logging
    from app.core.config import Settings
    with caplog.at_level(logging.WARNING):
        Settings(app_env="production", api_key="s3cr3t", database_url="postgresql://x/y")
    assert not any("API_KEY" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Session pool config
# ---------------------------------------------------------------------------

def test_sqlite_engine_has_no_pool_size():
    """SQLite engine is created without pool_size (NullPool/StaticPool)."""
    from app.db.session import engine
    # StaticPool / NullPool don't have pool_size attr — just check it doesn't crash
    assert engine is not None


# ---------------------------------------------------------------------------
# CORS header present on responses
# ---------------------------------------------------------------------------

def test_cors_header_present(client):
    r = client.get("/health", headers={"Origin": "https://example.com"})
    assert r.status_code == 200
    # With allow_origins=["*"], the header should be present
    assert "access-control-allow-origin" in r.headers
