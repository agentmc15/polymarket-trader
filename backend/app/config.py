"""Application configuration using Pydantic Settings."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "Polymarket Trader"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    secret_key: SecretStr = Field(default=SecretStr("change-me-in-production"))

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/polymarket",
        alias="DATABASE_URL",
    )
    database_pool_size: int = 5
    database_max_overflow: int = 10

    # Redis
    redis_url: str = Field(
        default="redis://localhost:6379",
        alias="REDIS_URL",
    )

    # Polymarket API
    polymarket_private_key: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_PRIVATE_KEY",
    )
    polymarket_funder_address: str = Field(
        default="",
        alias="POLYMARKET_FUNDER_ADDRESS",
    )
    polymarket_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_API_KEY",
    )
    polymarket_api_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_API_SECRET",
    )
    polymarket_api_passphrase: SecretStr = Field(
        default=SecretStr(""),
        alias="POLYMARKET_API_PASSPHRASE",
    )

    # CLOB API
    clob_api_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137  # Polygon mainnet

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Celery
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0",
        alias="CELERY_BROKER_URL",
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/0",
        alias="CELERY_RESULT_BACKEND",
    )

    @property
    def async_database_url(self) -> str:
        """Get async database URL."""
        if "+asyncpg" not in self.database_url:
            return self.database_url.replace("postgresql://", "postgresql+asyncpg://")
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
