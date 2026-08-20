"""Typed application settings, loaded once from the environment."""
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://marketpulse:marketpulse@localhost:5432/marketpulse"
    database_url_ro: str = (
        "postgresql+asyncpg://marketpulse_ai_ro:marketpulse_ai_ro@localhost:5432/marketpulse"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 5000
    db_ro_statement_timeout_ms: int = 3000

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_latest: int = 2
    cache_ttl_ohlcv: int = 30

    # Ingestion
    # NoDecode: pydantic-settings would otherwise try to JSON-parse these from
    # the env. We want plain comma-separated values, split by the validator below.
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )
    binance_ws_url: str = "wss://stream.binance.com:9443/stream"
    binance_rest_url: str = "https://api.binance.com"
    ingest_queue_maxsize: int = 20_000
    ingest_batch_size: int = 500
    ingest_batch_interval_ms: int = 250
    aggregator_interval_s: int = 20
    partition_ahead_days: int = 3
    retention_days: int = 30

    # API
    api_keys: str = "demo-key-public:public,demo-key-admin:admin"
    rate_limit_per_minute: int = 120
    max_page_size: int = 1000
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # AI
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5"
    ai_effort: str = "medium"
    ai_max_tokens: int = 8000
    ai_max_tool_iterations: int = 6
    ai_max_range_days: int = 7
    ai_max_rows: int = 5000

    # Ops
    log_level: str = "INFO"
    env: str = "development"

    @field_validator("symbols", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def api_key_map(self) -> dict[str, str]:
        """`key:role` pairs -> {key: role}. Roles: public | admin."""
        out: dict[str, str] = {}
        for pair in self.api_keys.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, _, role = pair.partition(":")
            out[key.strip()] = (role or "public").strip()
        return out

    @property
    def symbol_set(self) -> set[str]:
        return set(self.symbols)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
