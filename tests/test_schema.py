from __future__ import annotations

import inspect
from pathlib import Path

from bn_monitor import models


def test_hypertable_unique_constraints_include_time_column() -> None:
    for table in (models.alerts, models.liquidation_snapshots):
        primary_key_columns = {column.name for column in table.primary_key.columns}
        assert "ts" in primary_key_columns
        assert "id" in primary_key_columns


def test_healthcheck_command_checks_hypertables() -> None:
    from bn_monitor.cli import _healthcheck

    source = inspect.getsource(_healthcheck)
    assert "timescaledb_information.hypertables" in source
    assert "missing hypertables" in source
    assert "alembic_version" in source
    assert "0002_contract_type_symbols" in source


def test_alert_delivery_update_uses_full_hypertable_primary_key() -> None:
    from bn_monitor import repository

    source = inspect.getsource(repository.update_alert_delivery)
    assert "alerts.c.ts == alert_ts" in source
    assert "alerts.c.id == alert_id" in source


def test_repository_can_expire_stale_alerts() -> None:
    from bn_monitor import repository

    source = inspect.getsource(repository.expire_stale_alerts)
    assert 'state="expired"' in source
    assert '"open", "escalated"' in source


def test_alert_state_transitions_are_mode_scoped() -> None:
    from bn_monitor import repository

    find_source = inspect.getsource(repository.find_recent_active_alert)
    resolve_source = inspect.getsource(repository.resolve_inactive_alerts)
    assert "alerts.c.mode == mode" in find_source
    assert "alerts.c.mode == mode" in resolve_source


def test_alembic_uses_sync_psycopg_url() -> None:
    from bn_monitor.config import sync_database_url

    assert (
        sync_database_url("postgresql+asyncpg://u:p@localhost/db")
        == "postgresql+psycopg://u:p@localhost/db"
    )
    assert sync_database_url("postgresql://u:p@localhost/db") == "postgresql+psycopg://u:p@localhost/db"


def test_compose_app_uses_service_database_host() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "DATABASE_URL: postgresql+asyncpg://bn_monitor:bn_monitor@timescaledb:5432/bn_monitor" in compose
    assert 'command: ["sh", "-c", "alembic upgrade head && bn-monitor run"]' in compose
    assert 'test: ["CMD", "bn-monitor", "healthcheck"]' in compose
    assert 'max-size: "10m"' in compose
    assert 'max-file: "5"' in compose


def test_migration_has_replay_and_history_indexes() -> None:
    migration = Path("alembic/versions/0001_initial.py").read_text()
    assert "ix_alerts_replay" in migration
    assert "ix_futures_kline_1m_symbol_ts" in migration
    assert "ix_indicator_snapshot_1m_symbol_ts" in migration
    assert "price_move_norm_15m" in migration
    assert "oi_move_norm_15m" in migration
    assert "contract_type" in migration


def test_native_sql_uses_expanding_symbol_filters() -> None:
    for path in Path("bn_monitor").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "ANY(:symbols)" not in source


def test_alert_summary_command_exists() -> None:
    from bn_monitor.cli import main

    source = inspect.getsource(main)
    assert "alert-summary" in source
    assert "alert-projection" in source
    assert "alert-show" in source
    assert "--symbol" in source
    assert "--alert-type" in source
    assert "--severity" in source


def test_retention_command_exists_and_uses_configured_days() -> None:
    from bn_monitor.cli import _config_payload, main
    from bn_monitor.config import Settings

    source = inspect.getsource(main)
    assert "retention-run" in source
    assert "_retention_run(settings)" in source
    payload = _config_payload(Settings())
    assert payload["thresholds"]["data_retention_days"] == 30


def test_retention_tables_are_limited_to_time_series_data() -> None:
    from bn_monitor.cli import RETENTION_TABLES, _retention_run

    expected = {
        "futures_kline_1m",
        "futures_open_interest",
        "futures_mark_price",
        "liquidation_snapshots",
        "indicator_snapshot_1m",
    }
    assert expected <= set(RETENTION_TABLES)
    assert "symbols" not in RETENTION_TABLES
    assert "alert_cooldowns" not in RETENTION_TABLES
    source = inspect.getsource(_retention_run)
    assert "DELETE FROM {table} WHERE ts < :cutoff" in source
    assert '"cutoff": cutoff' in source


def test_config_dump_redacts_secrets_and_exposes_thresholds() -> None:
    from bn_monitor.cli import _config_payload
    from bn_monitor.config import Settings

    payload = _config_payload(
        Settings(
            database_url="postgresql+asyncpg://user:secret@localhost/db",
            discord_webhook_url="https://discord.example/webhook",
        )
    )
    assert payload["database_url"] == "postgresql+asyncpg://user:***@localhost/db"
    assert payload["discord_webhook_configured"] is True
    assert "discord.example" not in str(payload)
    assert payload["thresholds"]["price_threshold_bps"] == 35
    assert payload["thresholds"]["flat_oi_volume_percentile_threshold"] == 0.9
    assert payload["thresholds"]["data_retention_days"] == 30
    assert payload["intervals"]["rest_max_requests_per_second"] == 15
    assert payload["intervals"]["open_interest_poll_interval_seconds"] == 300
    assert payload["intervals"]["indicator_poll_interval_seconds"] == 5


def test_indicator_loop_interval_supports_latency_requirement() -> None:
    from bn_monitor.app import MonitorApp
    from bn_monitor.config import Settings

    assert Settings().indicator_poll_interval_seconds <= 10
    source = inspect.getsource(MonitorApp.run_indicator_loop)
    assert "indicator_poll_interval_seconds" in source


def test_websocket_message_failures_do_not_stop_app_loop() -> None:
    from bn_monitor.app import MonitorApp

    source = inspect.getsource(MonitorApp.run_ws)
    assert "binance_ws_message_failed" in source
    assert "handle_ws_message" in source
    assert "binance_ws_flush_failed" in source


def test_rest_poller_seeds_funding_history() -> None:
    from bn_monitor.binance import BinanceRestClient, poll_rest_forever, seed_funding_history_once

    assert hasattr(BinanceRestClient, "funding_rate_history")
    source = inspect.getsource(seed_funding_history_once)
    assert "funding_rate_history" in source
    assert "upsert_funding_history" in source
    assert "binance_funding_history_symbol_seed_failed" in source
    assert "binance_funding_history_seed_incomplete" in source
    assert "return True" in source
    assert "return False" in source
    poller_source = inspect.getsource(poll_rest_forever)
    assert "funding_history_seeded" in poller_source


def test_rest_poller_backfills_recent_closed_klines() -> None:
    from bn_monitor.binance import BinanceRestClient, poll_rest_forever, poll_symbol_public_context

    assert hasattr(BinanceRestClient, "klines_1m")
    source = inspect.getsource(poll_rest_forever)
    assert "poll_symbol_public_context" in source
    assert "open_interest_poll_interval_seconds" in source
    assert "poll_open_interest" in source
    assert "binance_rest_symbol_poll_failed" in source
    assert "symbols_payload = await rest.exchange_info()" in source
    symbol_source = inspect.getsource(poll_symbol_public_context)
    assert "klines_1m" in symbol_source
    assert "upsert_kline" in symbol_source
    assert "open_interest_value" in symbol_source


def test_env_example_covers_runtime_settings() -> None:
    env = Path(".env.example").read_text()
    expected_keys = [
        "DATABASE_URL",
        "BINANCE_REST_URL",
        "BINANCE_WS_URL",
        "SYMBOLS",
        "UNIVERSE_MODE",
        "EXCLUDED_SYMBOLS",
        "ALERT_MODE",
        "DISCORD_WEBHOOK_URL",
        "DATA_RETENTION_DAYS",
        "KLINE_GAP_MAX_RATIO",
        "KLINE_MAX_STALENESS_MINUTES",
        "MARKET_DATA_MAX_STALENESS_MINUTES",
        "REST_POLL_INTERVAL_SECONDS",
        "INDICATOR_POLL_INTERVAL_SECONDS",
        "KLINE_BACKFILL_LIMIT",
        "DISCORD_CAPACITY",
        "DISCORD_REFILL_PER_SECOND",
        "DIGEST_TRIGGER_COUNT",
        "REST_MAX_REQUESTS_PER_SECOND",
        "OPEN_INTEREST_POLL_INTERVAL_SECONDS",
        "WS_KLINE_STREAM_CHUNK_SIZE",
        "FLAT_OI_VOLUME_PERCENTILE_THRESHOLD",
        "FLAT_OI_MIN_OI_CHANGE_BPS",
        "NORMALIZED_MOVE_MIN_MAD_BPS",
        "MAX_LIVE_ALERTS_PER_CYCLE",
    ]
    for key in expected_keys:
        assert f"{key}=" in env


def test_implementation_audit_tracks_external_gates() -> None:
    audit = Path("docs/implementation-audit.md").read_text()
    assert "docker compose up -d --build" in audit
    assert "bn-monitor test-discord" in audit
    assert "24-hour data-quality" in audit
    assert "7-day stability run" in audit
