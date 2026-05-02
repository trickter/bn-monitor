from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://bn_monitor:bn_monitor@localhost:5432/bn_monitor"
    binance_rest_url: str = "https://fapi.binance.com"
    binance_ws_url: str = "wss://fstream.binance.com"
    universe_mode: Literal["configured", "all_usdt_perpetual"] = "configured"
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
    excluded_symbols: Annotated[list[str], NoDecode] = Field(default_factory=list)
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
    flat_oi_volume_percentile_threshold: float = 0.90
    flat_oi_min_oi_change_bps: float = 150
    normalized_move_min_mad_bps: float = 1
    alert_cooldown_minutes: dict[str, int] = Field(
        default_factory=lambda: {
            "CRITICAL": 5,
            "WARNING": 10,
            "flat_oi_buildup": 60,
        }
    )
    max_live_alerts_per_cycle: int = 5
    rest_poll_interval_seconds: int = 60
    rest_max_requests_per_second: float = 15
    indicator_poll_interval_seconds: int = 5
    ws_flush_interval_seconds: float = 0.2
    ws_kline_stream_chunk_size: int = 300
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

    @field_validator("excluded_symbols", mode="before")
    @classmethod
    def parse_excluded_symbols(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [part.strip().upper() for part in value.split(",") if part.strip()]
        return value  # type: ignore[return-value]

    @field_validator("alert_cooldown_minutes", mode="before")
    @classmethod
    def parse_alert_cooldowns(cls, value: object) -> dict[str, int]:
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("alert_cooldown_minutes must be a JSON object")
            return {str(key): int(minutes) for key, minutes in parsed.items()}
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
