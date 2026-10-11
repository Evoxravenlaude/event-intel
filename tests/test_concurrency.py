"""
Regressions from the 2026-10-11 production log:
  - two scheduler jobs clustered the same signals concurrently
  - a geocode-cache unique violation poisoned the transaction (PendingRollbackError)
"""
from __future__ import annotations
import threading

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.event import Event, EventEvidence, GeocodeCache, RawSignal, Venue
from app.services import geocoding
from app.services.clustering import cluster_signals


def test_geocode_cache_race_does_not_poison_the_session(db_session, monkeypatch):
    # Another worker already cached this key…
    db_session.add(GeocodeCache(key="k1", query="6.43,3.41", result=None))
    db_session.commit()
    # …but our existence check "missed" it (it committed between our check and our insert)
    real_select = geocoding.select
    monkeypatch.setattr(geocoding, "select", lambda *a: real_select(*a).where(GeocodeCache.key == "__never__"))

    db_session.add(Venue(name="Pending venue in the same transaction"))
    db_session.flush()
    geocoding._cache_put(db_session, "k1", "6.43,3.41", geocoding.Place(latitude=6.43, longitude=3.41, city="Eti Osa"))

    # Session still usable and the outer work survives
    monkeypatch.setattr(geocoding, "select", real_select)
    db_session.commit()
    assert db_session.execute(select(func.count()).select_from(Venue)).scalar_one() == 1
    assert db_session.execute(select(func.count()).select_from(GeocodeCache)).scalar_one() == 1


def test_provider_ok_but_db_error_does_not_trip_breaker(db_session, monkeypatch):
    from app.core import config as cfg
    monkeypatch.setattr(cfg.settings, "geocoding_enabled", True)
    geocoding.reset_breaker()
    monkeypatch.setattr(geocoding, "_provider_reverse",
                        lambda lat, lng: geocoding.Place(latitude=lat, longitude=lng, city="Eti Osa", country_code="NG"))
    def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(geocoding, "_cache_put", boom)
    try:
        geocoding.reverse(db_session, 6.43, 3.41)
    except RuntimeError:
        pass
    assert geocoding._provider_available() is True    # DB trouble is not the geocoder's fault


def test_concurrent_cluster_passes_do_not_duplicate_events(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/c.db", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False)
    with S() as s:
        words = ["quartz", "maple", "falcon", "cobalt", "saffron", "tundra", "velvet", "harbor", "ember", "glacier",
                 "orchid", "basalt", "meadow", "lantern", "comet", "willow", "canyon", "pepper", "violet", "summit"]
        for i, w in enumerate(words):
            s.add(RawSignal(source_type="luma", external_id=f"x{i}", title=f"{w}", source_confidence=0.8))
        s.commit()

    errors: list[Exception] = []
    start = threading.Barrier(4)

    def worker():
        db = S()
        try:
            start.wait()
            cluster_signals(db)
        except Exception as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    with S() as s:
        assert s.execute(select(func.count()).select_from(RawSignal).where(RawSignal.processed.is_(False))).scalar_one() == 0
        per_signal = s.execute(select(EventEvidence.raw_signal_id, func.count()).group_by(EventEvidence.raw_signal_id)).all()
        assert len(per_signal) == 20 and all(c == 1 for _, c in per_signal)   # every signal attached exactly once
        assert s.execute(select(func.count()).select_from(Event)).scalar_one() <= 20
    eng.dispose()
