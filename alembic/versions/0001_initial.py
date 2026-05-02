from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "symbols",
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("market_type", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("base_asset", sa.Text(), nullable=False),
        sa.Column("quote_asset", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tick_size", sa.Numeric(38, 18)),
        sa.Column("step_size", sa.Numeric(38, 18)),
        sa.Column("min_notional", sa.Numeric(38, 18)),
        sa.Column("tier", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("exchange", "market_type", "symbol"),
    )

    op.create_table(
        "futures_kline_1m",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("open", sa.Numeric(38, 18), nullable=False),
        sa.Column("high", sa.Numeric(38, 18), nullable=False),
        sa.Column("low", sa.Numeric(38, 18), nullable=False),
        sa.Column("close", sa.Numeric(38, 18), nullable=False),
        sa.Column("base_volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("quote_volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("taker_buy_base_volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("taker_buy_quote_volume", sa.Numeric(38, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts", "symbol"),
    )
    op.execute("SELECT create_hypertable('futures_kline_1m', 'ts', if_not_exists => TRUE)")
    op.create_index("ix_futures_kline_1m_symbol_ts", "futures_kline_1m", ["symbol", sa.text("ts DESC")])

    op.create_table(
        "futures_open_interest",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("open_interest", sa.Numeric(38, 18), nullable=False),
        sa.Column("open_interest_value", sa.Numeric(38, 18)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts", "symbol"),
    )
    op.execute("SELECT create_hypertable('futures_open_interest', 'ts', if_not_exists => TRUE)")
    op.create_index(
        "ix_futures_open_interest_symbol_ts", "futures_open_interest", ["symbol", sa.text("ts DESC")]
    )

    op.create_table(
        "futures_mark_price",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("mark_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("index_price", sa.Numeric(38, 18)),
        sa.Column("funding_rate", sa.Numeric(38, 18)),
        sa.Column("next_funding_time", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts", "symbol"),
    )
    op.execute("SELECT create_hypertable('futures_mark_price', 'ts', if_not_exists => TRUE)")
    op.create_index(
        "ix_futures_mark_price_symbol_ts", "futures_mark_price", ["symbol", sa.text("ts DESC")]
    )

    op.create_table(
        "liquidation_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("average_price", sa.Numeric(38, 18)),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("quote_value", sa.Numeric(38, 18), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts", "id"),
    )
    op.create_index("ix_liquidation_snapshots_ts_symbol", "liquidation_snapshots", ["ts", "symbol"])
    op.execute("SELECT create_hypertable('liquidation_snapshots', 'ts', if_not_exists => TRUE)")

    op.create_table(
        "market_factor_1m",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("btc_return_1m", sa.Numeric(38, 18)),
        sa.Column("eth_return_1m", sa.Numeric(38, 18)),
        sa.Column("market_median_return_1m", sa.Numeric(38, 18)),
        sa.Column("market_dispersion_1m", sa.Numeric(38, 18)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts"),
    )
    op.execute("SELECT create_hypertable('market_factor_1m', 'ts', if_not_exists => TRUE)")

    op.create_table(
        "indicator_snapshot_1m",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("return_1m", sa.Numeric(38, 18)),
        sa.Column("return_5m", sa.Numeric(38, 18)),
        sa.Column("return_15m", sa.Numeric(38, 18)),
        sa.Column("btc_relative_return_1m", sa.Numeric(38, 18)),
        sa.Column("beta_adjusted_return_1m", sa.Numeric(38, 18)),
        sa.Column("quote_volume_1m", sa.Numeric(38, 18)),
        sa.Column("volume_percentile", sa.Numeric(8, 6)),
        sa.Column("volume_robust_z", sa.Numeric(38, 18)),
        sa.Column("taker_buy_ratio", sa.Numeric(8, 6)),
        sa.Column("taker_sell_ratio", sa.Numeric(8, 6)),
        sa.Column("candle_body_ratio", sa.Numeric(8, 6)),
        sa.Column("candle_range_bps", sa.Numeric(38, 18)),
        sa.Column("oi_change_5m", sa.Numeric(38, 18)),
        sa.Column("oi_change_15m", sa.Numeric(38, 18)),
        sa.Column("oi_robust_z", sa.Numeric(38, 18)),
        sa.Column("funding_rate", sa.Numeric(38, 18)),
        sa.Column("funding_percentile", sa.Numeric(8, 6)),
        sa.Column("price_move_norm_15m", sa.Numeric(38, 18)),
        sa.Column("oi_move_norm_15m", sa.Numeric(38, 18)),
        sa.Column("price_spike_score", sa.Numeric(38, 18)),
        sa.Column("flat_oi_buildup_score", sa.Numeric(38, 18)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts", "symbol"),
    )
    op.execute("SELECT create_hypertable('indicator_snapshot_1m', 'ts', if_not_exists => TRUE)")
    op.create_index(
        "ix_indicator_snapshot_1m_symbol_ts", "indicator_snapshot_1m", ["symbol", sa.text("ts DESC")]
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("alert_type", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(38, 18), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("delivery_status", sa.Text(), nullable=False),
        sa.Column("discord_sent_at", sa.DateTime(timezone=True)),
        sa.Column("parent_alert_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ts", "id"),
    )
    op.create_index("ix_alerts_lookup", "alerts", ["ts", "symbol", "alert_type", "severity"])
    op.create_index(
        "ix_alerts_replay",
        "alerts",
        ["symbol", "alert_type", "severity", sa.text("ts DESC")],
    )
    op.create_index(
        "uq_alerts_source_signal",
        "alerts",
        ["ts", "symbol", "alert_type", "mode"],
        unique=True,
    )
    op.execute("SELECT create_hypertable('alerts', 'ts', if_not_exists => TRUE)")

    op.create_table(
        "alert_cooldowns",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_score", sa.Numeric(38, 18), nullable=False),
        sa.Column("count_1h", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("alert_cooldowns")
    op.drop_index("uq_alerts_source_signal", table_name="alerts")
    op.drop_index("ix_alerts_replay", table_name="alerts")
    op.drop_index("ix_alerts_lookup", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_indicator_snapshot_1m_symbol_ts", table_name="indicator_snapshot_1m")
    op.drop_table("indicator_snapshot_1m")
    op.drop_table("market_factor_1m")
    op.drop_index("ix_liquidation_snapshots_ts_symbol", table_name="liquidation_snapshots")
    op.drop_table("liquidation_snapshots")
    op.drop_index("ix_futures_mark_price_symbol_ts", table_name="futures_mark_price")
    op.drop_table("futures_mark_price")
    op.drop_index("ix_futures_open_interest_symbol_ts", table_name="futures_open_interest")
    op.drop_table("futures_open_interest")
    op.drop_index("ix_futures_kline_1m_symbol_ts", table_name="futures_kline_1m")
    op.drop_table("futures_kline_1m")
    op.drop_table("symbols")
