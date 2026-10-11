from __future__ import annotations
import logging
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_name: str = "event-intel-api"
    app_env: str = "development"
    debug: bool = True
    port: int = 8000
    database_url: str = "sqlite:///./event_intel.db"
    default_radius_km: float = 10.0
    source_timeout_seconds: int = 20
    enable_mock_adapters: bool = True

    # Clustering
    cluster_distance_km: float = 1.5
    cluster_time_window_hours: int = 12
    # Signals scoring >= link threshold auto-link; between the two go to review;
    # below the review threshold seed a new event. See services/clustering.py.
    cluster_link_threshold: float = 0.55
    cluster_review_threshold: float = 0.35
    # Only consider events whose start_time is within this many days of the
    # signal's detected time (events with no start_time are always considered).
    cluster_candidate_window_days: int = 14
    # Hard vetoes: when both sides have coordinates / start times and they are
    # this far apart, they are different events no matter how alike the titles
    # ("Jazz Night" exists in every city, every week).
    cluster_max_link_distance_km: float = 50.0
    cluster_max_link_time_days: float = 3.0
    # Max unprocessed signals handled per cluster pass (one transaction).
    cluster_batch_size: int = 500
    # Confidence at which an event is "confirmed" (webhooks, stats, UI badge).
    confirmed_confidence_threshold: float = 0.75

    # Geocoding (text ⇄ coordinates). Nominatim-compatible endpoint; no key needed.
    # Nominatim's usage policy requires an identifying User-Agent and ≤1 req/s.
    geocoding_enabled: bool = True
    geocoder_base_url: str = "https://nominatim.openstreetmap.org"
    geocoder_user_agent: str = "event-intel/1.1 (set GEOCODER_USER_AGENT to your contact email)"
    geocoder_min_interval_seconds: float = 1.0
    geocoder_timeout_seconds: int = 8
    geocoder_backoff_seconds: int = 300

    # Discovery ("what's around me") — see services/discovery.py
    discover_default_radius_km: float = 25.0
    discover_max_radius_km: float = 200.0
    # An area scanned within this many minutes is served from the DB without re-scanning
    discover_stale_minutes: int = 360
    # Which sources a live scan runs. Only geo-capable adapters are used; others ignored.
    discover_sources: str = "luma,ticketmaster,seatgeek"
    # Scheduler: re-scan cells that were requested within the last N days
    coverage_rescan_days: int = 7

    # Geo-capable global sources
    # Luma city feeds — keyless, ~80 cities worldwide (see services/luma_places.py)
    luma_api_base_url: str = "https://api.lu.ma"
    # A Luma city page covers its metro area: a city counts for a request if its
    # centre is within radius + this many km.
    luma_place_reach_km: float = 40.0

    ticketmaster_api_key: str | None = None
    ticketmaster_base_url: str = "https://app.ticketmaster.com/discovery/v2"
    seatgeek_client_id: str | None = None
    seatgeek_base_url: str = "https://api.seatgeek.com/2"

    # Auth — leave blank to disable in development
    api_key: str | None = None

    # CORS — comma-separated list of allowed origins.
    # Default "*" is fine for a private API behind an API key.
    # Set to your frontend domain(s) in production: "https://yourapp.com"
    cors_origins_raw: str = "*"

    # Webhooks — comma-separated URLs to POST when an event becomes confirmed
    webhook_urls: str | None = None
    webhook_secret: str | None = None  # optional HMAC-SHA256 signature in X-Signature

    # Embeddings
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embeddings_enabled: bool = True

    # Source credentials
    eventbrite_private_token: str | None = None
    eventbrite_organization_id: str | None = None
    luma_feed_urls: str | None = None
    telegram_feed_urls: str | None = None
    x_bearer_token: str | None = None
    x_feed_urls: str | None = None
    # Comma-separated BCP-47 codes to restrict X search to, e.g. "en,fr,sw". Blank = all languages.
    x_languages: str | None = None
    linkedin_source_urls: str | None = None

    # Scheduler
    scheduler_sources: str | None = None
    # Run the scheduler inside the API process. Leave false when you deploy a
    # dedicated `scheduler` service, otherwise every source is ingested twice.
    scheduler_in_process: bool = False
    scheduler_tick_seconds: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("database_url")
    @classmethod
    def normalize_postgres_scheme(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @model_validator(mode="after")
    def validate_thresholds(self) -> "Settings":
        if not 0 <= self.cluster_review_threshold < self.cluster_link_threshold:
            raise ValueError("CLUSTER_REVIEW_THRESHOLD must be >= 0 and < CLUSTER_LINK_THRESHOLD")
        return self

    @model_validator(mode="after")
    def warn_insecure_production(self) -> "Settings":
        """Emit warnings when running in production with insecure defaults."""
        if self.app_env == "production":
            if not self.api_key:
                logger.warning(
                    "SECURITY WARNING: API_KEY is not set in production. "
                    "All endpoints are publicly accessible."
                )
            if self.enable_mock_adapters:
                logger.warning(
                    "ENABLE_MOCK_ADAPTERS is true in production — "
                    "mock signals will be created for sources with no credentials."
                )
            if self.database_url.startswith("sqlite"):
                logger.warning(
                    "DATABASE_URL points to SQLite in production. "
                    "Use a PostgreSQL (Supabase) connection string."
                )
            if self.debug:
                logger.warning("DEBUG is true in production — Swagger UI is exposed.")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origins(self) -> list[str]:
        if self.cors_origins_raw.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    def split_csv(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]


settings = Settings()
