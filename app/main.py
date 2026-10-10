"""
Event Intel API + Meridian frontend — application entry point.

Routes:
  /             → Meridian frontend (app/static/index.html)
  /static/*     → Static assets
  /events       → Event Intel API
  /signals      → Event Intel API
  /review-queue → Event Intel API
  /stats        → Dashboard aggregates
  /discover     → Location-first discovery (scan around a point)
  /geocode      → Place search / reverse geocoding
  /health       → Liveness (process is up)
  /ready        → Readiness (database reachable, migrations applied)

Startup sequence:
  1. Lifespan handler: SQLite create_all (dev only), embedding warm-up in a
     background thread (so /health answers immediately — the model download
     used to block startup and trip Railway's healthcheck), optional
     in-process scheduler (SCHEDULER_IN_PROCESS=true).
  2. Middleware: CORS → request logging → APIKeyMiddleware (/ and /static/* exempt).
  3. Routers + static files mounted.

Schema: managed by `alembic upgrade head` in scripts/bootstrap.sh.
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.discover import router as discover_router
from app.api.events import router as events_router
from app.api.review_queue import router as review_router
from app.api.signals import router as signals_router
from app.api.stats import router as stats_router
from app.core.auth import APIKeyMiddleware
from app.core.config import settings
from app.core.logging import configure_logging

STATIC_DIR = Path(__file__).parent / "static"

configure_logging(production=settings.app_env == "production")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _start_scheduler_thread() -> None:
    """
    Launch the scheduler in a daemon thread so it runs alongside uvicorn
    inside the same process. A new event loop is created for the thread
    because asyncio loops are not shared across threads.
    """
    def _run() -> None:
        from app.workers.scheduler import run_scheduler
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_scheduler(tick_seconds=settings.scheduler_tick_seconds))
        except Exception as exc:
            logger.error("Scheduler thread crashed: %s", exc)
        finally:
            loop.close()

    threading.Thread(target=_run, name="scheduler", daemon=True).start()
    logger.info("In-process scheduler thread started")


def _warm_up_embeddings_async() -> None:
    def _run() -> None:
        try:
            from app.services import embeddings as emb_svc
            emb_svc._get_model()
        except Exception as exc:  # pragma: no cover — depends on environment
            logger.warning("Embedding model warm-up failed (non-fatal): %s", exc)

    threading.Thread(target=_run, name="embedding-warmup", daemon=True).start()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (env=%s)", settings.app_name, settings.app_env)

    if settings.is_sqlite:
        from app.db.base import Base
        from app.db.session import engine
        Base.metadata.create_all(bind=engine)
        logger.info("SQLite: tables created via create_all()")

    if settings.embeddings_enabled:
        _warm_up_embeddings_async()

    if settings.scheduler_sources and settings.scheduler_in_process:
        try:
            _start_scheduler_thread()
        except Exception as exc:
            logger.warning("Scheduler failed to start (non-fatal): %s", exc)
    elif settings.scheduler_sources:
        logger.info("SCHEDULER_SOURCES set but SCHEDULER_IN_PROCESS=false — expecting a separate scheduler service")
    else:
        logger.info("SCHEDULER_SOURCES not set — scheduler not started")

    if (STATIC_DIR / "index.html").exists():
        logger.info("Meridian frontend available at /")
    else:
        logger.warning("app/static/index.html not found — frontend not served")

    logger.info("Startup complete")
    yield
    logger.info("Shutting down %s", settings.app_name)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    if not request.url.path.startswith(("/static/", "/health")):
        logger.info(
            "%s %s → %d (%.1fms)", request.method, request.url.path, response.status_code, duration_ms,
            extra={"request_id": request_id, "path": request.url.path, "method": request.method,
                   "status_code": response.status_code, "duration_ms": duration_ms},
        )
    return response


app.add_middleware(APIKeyMiddleware)
# CORS must be the outermost middleware so 401s from the API-key check still
# carry CORS headers (otherwise the browser reports an opaque network error).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    logger.warning("Integrity error on %s: %s", request.url.path, exc.orig)
    return JSONResponse(status_code=409, content={"detail": "Conflict: a record with these unique fields already exists"})


# API routers (prefixed — always take priority over static catch-all)
app.include_router(events_router)
app.include_router(signals_router)
app.include_router(review_router)
app.include_router(stats_router)
app.include_router(discover_router)


@app.get("/health", tags=["meta"])
def health():
    """Liveness: the process is up. Does not touch the database."""
    return {"ok": True, "app": settings.app_name, "env": settings.app_env, "version": app.version}


@app.get("/ready", tags=["meta"])
def ready():
    """Readiness: the database answers and the schema is present."""
    from app.db.session import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            has_schema = conn.execute(text("SELECT COUNT(*) FROM events")).scalar_one() is not None
    except Exception as exc:
        logger.warning("Readiness check failed: %s", exc)
        return JSONResponse(status_code=503, content={"ok": False, "database": "unreachable", "detail": str(exc)[:200]})
    from app.services import embeddings as emb_svc
    return {
        "ok": True,
        "database": "ok",
        "schema": "ok" if has_schema else "missing",
        "embeddings": "loaded" if emb_svc._model is not None else ("disabled" if not settings.embeddings_enabled else "loading"),
    }


# Serve frontend at root — only if index.html exists
@app.get("/", include_in_schema=False)
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Frontend not found. Place the Meridian build at app/static/index.html"}, status_code=404)


# Mount /static for any additional assets (CSS, JS, images)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
