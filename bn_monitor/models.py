from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Identity,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

symbols = Table(
    "symbols",
    metadata,
    Column("exchange", Text, primary_key=True),
    Column("market_type", Text, primary_key=True),
    Column("symbol", Text, primary_key=True),
    Column("base_asset", Text, nullable=False),
    Column("quote_asset", Text, nullable=False),
    Column("contract_type", Text),
    Column("status", Text, nullable=False),
    Column("tick_size", Numeric(38, 18)),
    Column("step_size", Numeric(38, 18)),
    Column("min_notional", Numeric(38, 18)),
    Column("tier", Integer, nullable=False, server_default="0"),
    Column("is_active", Boolean, nullable=False, server_default=func.true()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

futures_kline_1m = Table(
    "futures_kline_1m",
    metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("symbol", Text, primary_key=True),
    Column("open", Numeric(38, 18), nullable=False),
    Column("high", Numeric(38, 18), nullable=False),
    Column("low", Numeric(38, 18), nullable=False),
    Column("close", Numeric(38, 18), nullable=False),
    Column("base_volume", Numeric(38, 18), nullable=False),
    Column("quote_volume", Numeric(38, 18), nullable=False),
    Column("trade_count", Integer, nullable=False),
    Column("taker_buy_base_volume", Numeric(38, 18), nullable=False),
    Column("taker_buy_quote_volume", Numeric(38, 18), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

futures_open_interest = Table(
    "futures_open_interest",
    metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("symbol", Text, primary_key=True),
    Column("open_interest", Numeric(38, 18), nullable=False),
    Column("open_interest_value", Numeric(38, 18)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

futures_mark_price = Table(
    "futures_mark_price",
    metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("symbol", Text, primary_key=True),
    Column("mark_price", Numeric(38, 18), nullable=False),
    Column("index_price", Numeric(38, 18)),
    Column("funding_rate", Numeric(38, 18)),
    Column("next_funding_time", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

liquidation_snapshots = Table(
    "liquidation_snapshots",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("price", Numeric(38, 18), nullable=False),
    Column("average_price", Numeric(38, 18)),
    Column("quantity", Numeric(38, 18), nullable=False),
    Column("quote_value", Numeric(38, 18), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

market_factor_1m = Table(
    "market_factor_1m",
    metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("btc_return_1m", Numeric(38, 18)),
    Column("eth_return_1m", Numeric(38, 18)),
    Column("market_median_return_1m", Numeric(38, 18)),
    Column("market_dispersion_1m", Numeric(38, 18)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

indicator_snapshot_1m = Table(
    "indicator_snapshot_1m",
    metadata,
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("symbol", Text, primary_key=True),
    Column("return_1m", Numeric(38, 18)),
    Column("return_5m", Numeric(38, 18)),
    Column("return_15m", Numeric(38, 18)),
    Column("btc_relative_return_1m", Numeric(38, 18)),
    Column("beta_adjusted_return_1m", Numeric(38, 18)),
    Column("quote_volume_1m", Numeric(38, 18)),
    Column("volume_percentile", Numeric(8, 6)),
    Column("volume_robust_z", Numeric(38, 18)),
    Column("taker_buy_ratio", Numeric(8, 6)),
    Column("taker_sell_ratio", Numeric(8, 6)),
    Column("candle_body_ratio", Numeric(8, 6)),
    Column("candle_range_bps", Numeric(38, 18)),
    Column("oi_change_5m", Numeric(38, 18)),
    Column("oi_change_15m", Numeric(38, 18)),
    Column("oi_robust_z", Numeric(38, 18)),
    Column("funding_rate", Numeric(38, 18)),
    Column("funding_percentile", Numeric(8, 6)),
    Column("price_move_norm_15m", Numeric(38, 18)),
    Column("oi_move_norm_15m", Numeric(38, 18)),
    Column("price_spike_score", Numeric(38, 18)),
    Column("flat_oi_buildup_score", Numeric(38, 18)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

alerts = Table(
    "alerts",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("ts", DateTime(timezone=True), primary_key=True),
    Column("symbol", Text, nullable=False),
    Column("alert_type", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("direction", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("score", Numeric(38, 18), nullable=False),
    Column("title", Text, nullable=False),
    Column("message", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("mode", Text, nullable=False),
    Column("delivery_status", Text, nullable=False),
    Column("discord_sent_at", DateTime(timezone=True)),
    Column("parent_alert_id", BigInteger),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

alert_cooldowns = Table(
    "alert_cooldowns",
    metadata,
    Column("key", Text, primary_key=True),
    Column("last_sent_at", DateTime(timezone=True), nullable=False),
    Column("last_score", Numeric(38, 18), nullable=False),
    Column("count_1h", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
