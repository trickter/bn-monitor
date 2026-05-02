from __future__ import annotations

import asyncio
from typing import Any

import structlog

from bn_monitor.alerts import (
    AlertService,
    process_latest_indicator_alerts,
    process_latest_liquidation_confirmations,
)
from bn_monitor import repository
from bn_monitor.binance import BinanceRestClient, BinanceStream, handle_ws_message, poll_rest_forever
from bn_monitor.config import Settings
from bn_monitor.db import create_engine, create_sessionmaker
from bn_monitor.discord import DiscordWebhook
from bn_monitor.indicators import compute_latest_indicators

log = structlog.get_logger(__name__)


class MonitorApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(settings.database_url)
        self.session_factory = create_sessionmaker(self.engine)
        self._symbol_set = frozenset(symbol.upper() for symbol in settings.symbols)

    async def run(self) -> None:
        rest = BinanceRestClient(self.settings.binance_rest_url)
        try:
            await asyncio.gather(
                self.run_ws(),
                poll_rest_forever(
                    rest,
                    self.settings.symbols,
                    self.session_factory,
                    self.settings.rest_poll_interval_seconds,
                    self.settings.kline_backfill_limit,
                ),
                self.run_indicator_loop(),
            )
        finally:
            await rest.close()
            await self.engine.dispose()

    async def run_ws(self) -> None:
        stream = BinanceStream(self.settings.binance_ws_url, self.settings.symbols)
        pending: dict[str, list[dict[str, Any]]] = {
            "kline": [],
            "mark_price": [],
            "liquidation": [],
        }

        async def collect(kind: str, row: dict[str, Any]) -> None:
            if kind != "kline" and row["symbol"] not in self._symbol_set:
                return
            pending[kind].append(row)

        async def flush() -> None:
            klines = pending["kline"]
            marks = pending["mark_price"]
            liqs = pending["liquidation"]
            pending["kline"] = []
            pending["mark_price"] = []
            pending["liquidation"] = []
            if not (klines or marks or liqs):
                return
            async with self.session_factory() as session:
                async with session.begin():
                    for row in klines:
                        await repository.upsert_kline(session, row)
                    for row in marks:
                        await repository.upsert_mark_price(session, row)
                    for row in liqs:
                        await repository.insert_liquidation_snapshot(session, row)

        async def reader() -> None:
            async for message in stream.messages():
                try:
                    await handle_ws_message(message, collect)
                except Exception as exc:
                    log.warning("binance_ws_message_failed", error=str(exc))

        async def flusher() -> None:
            while True:
                await asyncio.sleep(self.settings.ws_flush_interval_seconds)
                try:
                    await flush()
                except Exception as exc:
                    log.warning("binance_ws_flush_failed", error=str(exc))

        await asyncio.gather(reader(), flusher())

    async def run_indicator_loop(self) -> None:
        discord = (
            DiscordWebhook(
                self.settings.discord_webhook_url,
                self.settings.discord_capacity,
                self.settings.discord_refill_per_second,
            )
            if self.settings.discord_webhook_url
            else None
        )
        alert_service = AlertService(self.settings, discord)
        try:
            while True:
                try:
                    async with self.session_factory() as session:
                        async with session.begin():
                            count = await compute_latest_indicators(session, self.settings.symbols)
                            alert_count = await process_latest_indicator_alerts(
                                session, self.settings, alert_service
                            )
                            alert_count += await process_latest_liquidation_confirmations(
                                session, self.settings, alert_service
                            )
                            if count:
                                log.info("indicators_computed", count=count, alerts=alert_count)
                except Exception as exc:
                    log.warning("indicator_loop_failed", error=str(exc))
                await asyncio.sleep(self.settings.indicator_poll_interval_seconds)
        finally:
            if discord:
                await discord.close()
