"""
Event Intel API + Meridian frontend — application entry point.

Routes:
  /           → Meridian frontend (app/static/index.html)
  /static/*   → Static assets
  /events     → Event Intel API
  /signals    → Event Intel API
  /review-queue → Event Intel API
  /health     → Health check

Startup sequence:
  1. Lifespan handler: logging, SQLite create_all (dev only), embedding warm-up.
  2. Middleware: CORS → APIKeyMiddleware (/ and /static/* are exempt).
  3. Routers + static files mounted.

Schema: managed by `alembic upgrade head` in scripts/bootstrap.sh.
"""
from __future__ import annotations
import logging
import logging.config
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.events import router as events_router
from app.api.review_queue import router as review_router
from app.api.signals import router as signals_router
from app.core.auth import APIKeyMiddleware
from app.core.config import settings

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "logging.Formatter",
            "fmt": '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":%(message)r}',
        },
        "plain": {
            "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if settings.app_env == "production" else "plain",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "uvicorn": {"level": "INFO"},
        "sqlalchemy.engine": {"level": "WARNING"},
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (env=%s)", settings.app_name, settings.app_env)

    if settings.database_url.startswith("sqlite"):
        from app.db.base import Base
        from app.db.session import engine
        Base.metadata.create_all(bind=engine)
        logger.info("SQLite: tables created via create_all()")

    if settings.embeddings_enabled:
        try:
            from app.services import embeddings as emb_svc
            emb_svc._get_model()
        except Exception as exc:
            logger.warning("Embedding model warm-up failed (non-fatal): %s", exc)

    frontend = STATIC_DIR / "index.html"
    if frontend.exists():
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
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
)
app.add_middleware(APIKeyMiddleware)

# API routers (prefixed — always take priority over static catch-all)
app.include_router(events_router)
app.include_router(signals_router)
app.include_router(review_router)


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "app": settings.app_name, "env": settings.app_env}


# Serve frontend at root — only if index.html exists
# Placed AFTER API routers so /events, /signals etc. are never caught here
@app.get("/", include_in_schema=False)
def serve_frontend():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"detail": "Frontend not found. Place meridian-full.html at app/static/index.html"}


# Mount /static for any additional assets (CSS, JS, images)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
