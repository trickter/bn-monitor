from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://bn_monitor:bn_monitor@localhost:5432/bn_monitor"
    binance_rest_url: str = "https://fapi.binance.com"
    binance_ws_url: str = "wss://fstream.binance.com"
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "BNBUSDT",
            "XRPUSDT",
            "DOGEUSDT",
            "ADAUSDT",
            "LINKUSDT",
            "AVAXUSDT",
            "LTCUSDT",
        ]
    )
    alert_mode: Literal["shadow", "live"] = "shadow"
    discord_webhook_url: str | None = None
    log_level: str = "INFO"
    kline_gap_max_ratio: float = 0.001
    kline_max_staleness_minutes: int = 3
    market_data_max_staleness_minutes: int = 5
    price_threshold_bps: float = 35
    small_move_threshold_bps: float = 10
    volume_percentile_threshold: float = 0.99
    volume_robust_z_threshold: float = 4
    oi_buildup_threshold: float = 2
    price_flat_norm_threshold: float = 0.5
    rest_poll_interval_seconds: int = 60
    indicator_poll_interval_seconds: int = 5
    ws_flush_interval_seconds: float = 0.2
    kline_backfill_limit: int = 180
    discord_capacity: int = 5
    discord_refill_per_second: float = 1.0
    digest_trigger_count: int = 5

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def sync_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url
