from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import bindparam, text

from bn_monitor.alerts import (
    AlertService,
    generate_alerts,
    process_latest_indicator_alerts,
    process_latest_liquidation_confirmations,
)
from bn_monitor.app import MonitorApp
from bn_monitor.config import get_settings
from bn_monitor.db import create_engine, create_sessionmaker
from bn_monitor.discord import DiscordWebhook
from bn_monitor.indicators import compute_latest_indicators
from bn_monitor.logging import configure_logging
from bn_monitor.quality import kline_gap_reports, table_freshness_reports
from bn_monitor.reports import alert_summary


def main() -> None:
    parser = argparse.ArgumentParser(prog="bn-monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("config-dump")
    sub.add_parser("healthcheck")
    sub.add_parser("test-discord")
    sub.add_parser("compute-indicators")
    sub.add_parser("generate-alerts")
    quality = sub.add_parser("data-quality")
    quality.add_argument("--lookback-hours", type=int, default=24)
    quality.add_argument("--max-staleness-minutes", type=int, default=None)
    alert_report = sub.add_parser("alert-summary")
    alert_report.add_argument("--lookback-hours", type=int, default=24)
    alert_report.add_argument("--symbol")
    alert_report.add_argument("--alert-type")
    alert_report.add_argument("--severity")
    projection = sub.add_parser("alert-projection")
    projection.add_argument("--hours", type=int, default=24)
    projection.add_argument("--profile", default="balanced")
    alert_show = sub.add_parser("alert-show")
    alert_show.add_argument("id", type=int)
    sub.add_parser("poll-once")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    if args.command == "run":
        asyncio.run(MonitorApp(settings).run())
    elif args.command == "config-dump":
        _config_dump(settings)
    elif args.command == "healthcheck":
        asyncio.run(_healthcheck(settings))
    elif args.command == "test-discord":
        asyncio.run(_test_discord(settings))
    elif args.command == "compute-indicators":
        asyncio.run(_compute_indicators(settings))
    elif args.command == "generate-alerts":
        asyncio.run(_generate_alerts(settings))
    elif args.command == "data-quality":
        asyncio.run(_data_quality(settings, args.lookback_hours, args.max_staleness_minutes))
    elif args.command == "alert-summary":
        asyncio.run(_alert_summary(settings, args.lookback_hours, args.symbol, args.alert_type, args.severity))
    elif args.command == "alert-projection":
        asyncio.run(_alert_projection(settings, args.hours, args.profile))
    elif args.command == "alert-show":
        asyncio.run(_alert_show(settings, args.id))
    elif args.command == "poll-once":
        asyncio.run(_poll_once(settings))


async def _test_discord(settings) -> None:
    if not settings.discord_webhook_url:
        raise SystemExit("DISCORD_WEBHOOK_URL is not configured")
    webhook = DiscordWebhook(
        settings.discord_webhook_url, settings.discord_capacity, settings.discord_refill_per_second
    )
    try:
        sent = await webhook.send_alert(
            {
                "ts": datetime.now(UTC),
                "symbol": "TEST",
                "direction": "neutral",
                "severity": "WARNING",
                "title": "Binance monitor test",
                "message": "Discord webhook test message.",
                "payload": {
                    "trigger_conditions": ["manual test"],
                    "btc_relative_return_1m": "0",
                    "volume_percentile": "0",
                    "volume_robust_z": "0",
                    "taker_buy_ratio": "0",
                    "oi_robust_z": "0",
                },
            },
            None,
        )
        if not sent:
            raise SystemExit("Discord webhook returned a non-success response")
    finally:
        await webhook.close()


async def _healthcheck(settings) -> None:
    required_tables = {
        "alembic_version",
        "symbols",
        "futures_kline_1m",
        "futures_open_interest",
        "futures_mark_price",
        "liquidation_snapshots",
        "market_factor_1m",
        "indicator_snapshot_1m",
        "alerts",
        "alert_cooldowns",
    }
    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name IN :tables
                        """
                    ).bindparams(bindparam("tables", expanding=True)),
                    {"tables": sorted(required_tables)},
                )
            ).scalars().all()
            hypertables = (
                await conn.execute(
                    text(
                        """
                        SELECT hypertable_name
                        FROM _timescaledb_catalog.hypertable
                        WHERE schema_name = 'public'
                          AND hypertable_name IN :hypertables
                        """
                    ).bindparams(bindparam("hypertables", expanding=True)),
                    {
                        "hypertables": [
                            "futures_kline_1m",
                            "futures_open_interest",
                            "futures_mark_price",
                            "liquidation_snapshots",
                            "market_factor_1m",
                            "indicator_snapshot_1m",
                            "alerts",
                        ]
                    },
                )
            ).scalars().all()
            alembic_revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            ).scalar_one_or_none()
        missing = sorted(required_tables - set(rows))
        if missing:
            raise SystemExit(f"missing tables: {', '.join(missing)}")
        if alembic_revision != "0002_add_contract_type_to_symbols":
            raise SystemExit(f"unexpected alembic revision: {alembic_revision}")
        missing_hypertables = sorted(
            {
                "futures_kline_1m",
                "futures_open_interest",
                "futures_mark_price",
                "liquidation_snapshots",
                "market_factor_1m",
                "indicator_snapshot_1m",
                "alerts",
            }
            - set(hypertables)
        )
        if missing_hypertables:
            raise SystemExit(f"missing hypertables: {', '.join(missing_hypertables)}")
        print("ok")
    finally:
        await engine.dispose()


async def _compute_indicators(settings) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    try:
        async with session_factory() as session:
            async with session.begin():
                count = await compute_latest_indicators(
                    session,
                    settings.symbols,
                    settings.market_data_max_staleness_minutes,
                    settings.normalized_move_min_mad_bps,
                )
                print(f"computed {count} indicator snapshots")
    finally:
        await engine.dispose()


async def _generate_alerts(settings) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    discord = (
        DiscordWebhook(settings.discord_webhook_url, settings.discord_capacity, settings.discord_refill_per_second)
        if settings.discord_webhook_url
        else None
    )
    service = AlertService(settings, discord)
    try:
        async with session_factory() as session:
            async with session.begin():
                count = await process_latest_indicator_alerts(session, settings, service)
                count += await process_latest_liquidation_confirmations(session, settings, service)
                print(f"stored {count} alerts")
    finally:
        if discord:
            await discord.close()
        await engine.dispose()


async def _data_quality(settings, lookback_hours: int, max_staleness_minutes: int | None = None) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    try:
        async with session_factory() as session:
            staleness_limit = (
                settings.kline_max_staleness_minutes
                if max_staleness_minutes is None
                else max_staleness_minutes
            )
            reports = await kline_gap_reports(
                session,
                settings.symbols,
                settings.kline_gap_max_ratio,
                lookback_hours,
                staleness_limit,
            )
            open_interest_reports = await table_freshness_reports(
                session,
                settings.symbols,
                "open_interest",
                settings.market_data_max_staleness_minutes,
            )
            mark_price_reports = await table_freshness_reports(
                session,
                settings.symbols,
                "mark_price",
                settings.market_data_max_staleness_minutes,
            )
        payload = {
            "klines": [report.__dict__ for report in reports],
            "open_interest": [report.__dict__ for report in open_interest_reports],
            "mark_price": [report.__dict__ for report in mark_price_reports],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if any(not report.ok for report in reports + open_interest_reports + mark_price_reports):
            raise SystemExit(2)
    finally:
        await engine.dispose()


async def _alert_summary(
    settings,
    lookback_hours: int,
    symbol: str | None = None,
    alert_type: str | None = None,
    severity: str | None = None,
) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    try:
        async with session_factory() as session:
            rows = await alert_summary(session, lookback_hours, symbol, alert_type, severity)
        print(json.dumps([row.__dict__ for row in rows], ensure_ascii=False, indent=2, default=str))
    finally:
        await engine.dispose()


async def _poll_once(settings) -> None:
    from bn_monitor import repository
    from bn_monitor.binance import BinanceRestClient, poll_symbol_public_context

    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    rest = BinanceRestClient(
        settings.binance_rest_url,
        min_interval_seconds=1 / settings.rest_max_requests_per_second,
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                await repository.upsert_symbols(session, await rest.exchange_info())
                for symbol in settings.symbols:
                    for row in await rest.funding_rate_history(symbol, limit=100):
                        await repository.upsert_funding_history(session, row)
                    await poll_symbol_public_context(
                        rest,
                        session,
                        symbol,
                        settings.kline_backfill_limit,
                    )
        print("poll complete")
    finally:
        await rest.close()
        await engine.dispose()


async def _alert_projection(settings, hours: int, profile: str) -> None:
    projected_settings = settings
    if profile != "balanced":
        raise SystemExit("only the balanced projection profile is currently supported")
    engine = create_engine(settings.database_url)
    session_factory = create_sessionmaker(engine)
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            i.*,
                            mf.btc_return_1m,
                            mf.market_median_return_1m,
                            CASE
                                WHEN i.return_1m IS NOT NULL
                                 AND mf.market_median_return_1m IS NOT NULL
                                THEN i.return_1m - mf.market_median_return_1m
                                ELSE NULL
                            END AS market_relative_return_1m
                        FROM indicator_snapshot_1m i
                        LEFT JOIN LATERAL (
                            SELECT btc_return_1m, market_median_return_1m
                            FROM market_factor_1m
                            WHERE ts <= i.ts
                            ORDER BY ts DESC
                            LIMIT 1
                        ) mf ON TRUE
                        WHERE i.ts >= now() - (:hours * interval '1 hour')
                          AND i.symbol IN :symbols
                        """
                    ).bindparams(bindparam("symbols", expanding=True)),
                    {"hours": hours, "symbols": settings.symbols},
                )
            ).mappings().all()
        counts: dict[str, int] = {}
        total = 0
        for row in rows:
            for decision in generate_alerts(dict(row), projected_settings):
                counts[decision.alert_type] = counts.get(decision.alert_type, 0) + 1
                total += 1
        print(json.dumps({"hours": hours, "profile": profile, "total": total, "by_type": counts}, indent=2))
    finally:
        await engine.dispose()


async def _alert_show(settings, alert_id: int) -> None:
    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT * FROM alerts WHERE id = :id LIMIT 1"), {"id": alert_id})
            ).mappings().first()
        if row is None:
            raise SystemExit(f"alert not found: {alert_id}")
        print(json.dumps(dict(row), ensure_ascii=False, indent=2, default=str))
    finally:
        await engine.dispose()


def _redact_url_secret(value: str) -> str:
    if "://" not in value or "@" not in value:
        return value
    scheme, rest = value.split("://", 1)
    credentials, host = rest.split("@", 1)
    if ":" not in credentials:
        return value
    user, _password = credentials.split(":", 1)
    return f"{scheme}://{user}:***@{host}"


def _config_payload(settings) -> dict[str, object]:
    return {
        "database_url": _redact_url_secret(settings.database_url),
        "binance_rest_url": settings.binance_rest_url,
        "binance_ws_url": settings.binance_ws_url,
        "universe_mode": settings.universe_mode,
        "symbols": settings.symbols,
        "excluded_symbols": settings.excluded_symbols,
        "alert_mode": settings.alert_mode,
        "discord_webhook_configured": bool(settings.discord_webhook_url),
        "log_level": settings.log_level,
        "thresholds": {
            "kline_gap_max_ratio": settings.kline_gap_max_ratio,
            "kline_max_staleness_minutes": settings.kline_max_staleness_minutes,
            "market_data_max_staleness_minutes": settings.market_data_max_staleness_minutes,
            "price_threshold_bps": settings.price_threshold_bps,
            "small_move_threshold_bps": settings.small_move_threshold_bps,
            "volume_percentile_threshold": settings.volume_percentile_threshold,
            "volume_robust_z_threshold": settings.volume_robust_z_threshold,
            "oi_buildup_threshold": settings.oi_buildup_threshold,
            "price_flat_norm_threshold": settings.price_flat_norm_threshold,
            "flat_oi_volume_percentile_threshold": settings.flat_oi_volume_percentile_threshold,
            "flat_oi_min_oi_change_bps": settings.flat_oi_min_oi_change_bps,
            "normalized_move_min_mad_bps": settings.normalized_move_min_mad_bps,
            "digest_trigger_count": settings.digest_trigger_count,
            "max_live_alerts_per_cycle": settings.max_live_alerts_per_cycle,
            "alert_cooldown_minutes": settings.alert_cooldown_minutes,
        },
        "intervals": {
            "rest_poll_interval_seconds": settings.rest_poll_interval_seconds,
            "rest_max_requests_per_second": settings.rest_max_requests_per_second,
            "indicator_poll_interval_seconds": settings.indicator_poll_interval_seconds,
            "ws_kline_stream_chunk_size": settings.ws_kline_stream_chunk_size,
            "kline_backfill_limit": settings.kline_backfill_limit,
        },
        "discord_rate_limit": {
            "capacity": settings.discord_capacity,
            "refill_per_second": settings.discord_refill_per_second,
        },
    }


def _config_dump(settings) -> None:
    print(json.dumps(_config_payload(settings), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
