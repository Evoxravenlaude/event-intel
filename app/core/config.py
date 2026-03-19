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
    cluster_distance_km: float = 1.5
    cluster_time_window_hours: int = 12
    source_timeout_seconds: int = 20
    enable_mock_adapters: bool = True

    # Auth — leave blank to disable in development
    api_key: str | None = None

    # CORS — comma-separated list of allowed origins.
    # Default "*" is fine for a private API behind an API key.
    # Set to your frontend domain(s) in production: "https://yourapp.com"
    cors_origins_raw: str = "*"

    # Webhooks — comma-separated URLs to POST when an event is confirmed
    webhook_urls: str | None = None

    # Embeddings
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    embeddings_enabled: bool = True

    # Source credentials
    eventbrite_private_token: str | None = None
    luma_feed_urls: str | None = None
    telegram_feed_urls: str | None = None
    x_bearer_token: str | None = None
    linkedin_source_urls: str | None = None

    # Scheduler
    scheduler_sources: str | None = None

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
        return self

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
