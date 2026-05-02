from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog
import websockets

from bn_monitor import repository

log = structlog.get_logger(__name__)


def ms_to_datetime(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def closed_kline_from_ws(message: dict[str, Any]) -> dict[str, Any] | None:
    data = message.get("data", message)
    kline = data.get("k", {})
    if not kline.get("x"):
        return None
    return {
        "ts": ms_to_datetime(kline["T"]),
        "symbol": kline["s"].upper(),
        "open": Decimal(kline["o"]),
        "high": Decimal(kline["h"]),
        "low": Decimal(kline["l"]),
        "close": Decimal(kline["c"]),
        "base_volume": Decimal(kline["v"]),
        "quote_volume": Decimal(kline["q"]),
        "trade_count": int(kline["n"]),
        "taker_buy_base_volume": Decimal(kline["V"]),
        "taker_buy_quote_volume": Decimal(kline["Q"]),
    }


def mark_price_from_ws(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": ms_to_datetime(item["E"]),
        "symbol": item["s"].upper(),
        "mark_price": Decimal(item["p"]),
        "index_price": Decimal(item["i"]),
        "funding_rate": Decimal(item["r"]),
        "next_funding_time": ms_to_datetime(item["T"]) if int(item.get("T", 0)) else None,
    }


def liquidation_from_ws(item: dict[str, Any]) -> dict[str, Any]:
    order = item.get("o", item)
    price = Decimal(order["p"])
    quantity = Decimal(order["q"])
    average_price = Decimal(order.get("ap") or order["p"])
    return {
        "ts": ms_to_datetime(order["T"]),
        "symbol": order["s"].upper(),
        "side": order["S"].upper(),
        "price": price,
        "average_price": average_price,
        "quantity": quantity,
        "quote_value": average_price * quantity,
        "raw": item,
    }


class BinanceRestClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        min_interval_seconds: float = 0.1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(base_url=self.base_url, timeout=10)
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def exchange_info(self) -> list[dict[str, Any]]:
        response = await self._get("/fapi/v1/exchangeInfo")
        payload = response.json()
        rows: list[dict[str, Any]] = []
        for item in payload["symbols"]:
            filters = {flt["filterType"]: flt for flt in item.get("filters", [])}
            rows.append(
                {
                    "exchange": "binance",
                    "market_type": "usdm_futures",
                    "symbol": item["symbol"],
                    "base_asset": item["baseAsset"],
                    "quote_asset": item["quoteAsset"],
                    "status": item["status"],
                    "tick_size": Decimal(filters.get("PRICE_FILTER", {}).get("tickSize", "0")),
                    "step_size": Decimal(filters.get("LOT_SIZE", {}).get("stepSize", "0")),
                    "min_notional": Decimal(filters.get("MIN_NOTIONAL", {}).get("notional", "0")),
                    "tier": 0,
                    "is_active": item["status"] == "TRADING",
                    "updated_at": datetime.now(UTC),
                }
            )
        return rows

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        response = await self._get("/fapi/v1/openInterest", params={"symbol": symbol})
        payload = response.json()
        return {
            "ts": ms_to_datetime(payload["time"]),
            "symbol": payload["symbol"].upper(),
            "open_interest": Decimal(payload["openInterest"]),
            "open_interest_value": None,
        }

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        response = await self._get("/fapi/v1/premiumIndex", params={"symbol": symbol})
        payload = response.json()
        return {
            "ts": ms_to_datetime(payload["time"]),
            "symbol": payload["symbol"].upper(),
            "mark_price": Decimal(payload["markPrice"]),
            "index_price": Decimal(payload["indexPrice"]),
            "funding_rate": Decimal(payload["lastFundingRate"]),
            "next_funding_time": ms_to_datetime(payload["nextFundingTime"])
            if int(payload.get("nextFundingTime", 0))
            else None,
        }

    async def klines_1m(
        self, symbol: str, limit: int = 180, now_ms: int | None = None
    ) -> list[dict[str, Any]]:
        response = await self._get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1m", "limit": limit},
        )
        current_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        rows: list[dict[str, Any]] = []
        for item in response.json():
            close_time = int(item[6])
            if close_time >= current_ms:
                continue
            rows.append(
                {
                    "ts": ms_to_datetime(close_time),
                    "symbol": symbol.upper(),
                    "open": Decimal(item[1]),
                    "high": Decimal(item[2]),
                    "low": Decimal(item[3]),
                    "close": Decimal(item[4]),
                    "base_volume": Decimal(item[5]),
                    "quote_volume": Decimal(item[7]),
                    "trade_count": int(item[8]),
                    "taker_buy_base_volume": Decimal(item[9]),
                    "taker_buy_quote_volume": Decimal(item[10]),
                }
            )
        return rows

    async def funding_rate_history(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        response = await self._get(
            "/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": limit},
        )
        payload = response.json()
        return [
            {
                "ts": ms_to_datetime(item["fundingTime"]),
                "symbol": item["symbol"].upper(),
                "mark_price": Decimal(item["markPrice"]),
                "index_price": None,
                "funding_rate": Decimal(item["fundingRate"]),
                "next_funding_time": None,
            }
            for item in payload
        ]

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(3):
            await self._wait_for_slot()
            response = await self.client.get(path, params=params)
            if response.status_code == 429:
                retry_after = _retry_after(response)
                log.warning("binance_rest_rate_limited", retry_after=retry_after, path=path)
                await asyncio.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600 and attempt < 2:
                await asyncio.sleep(2**attempt)
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

    async def _wait_for_slot(self) -> None:
        now = asyncio.get_running_loop().time()
        wait = self.min_interval_seconds - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_running_loop().time()


def _retry_after(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    return float(header) if header else 1.0


class BinanceStream:
    def __init__(self, ws_url: str, symbols: list[str]) -> None:
        self.ws_url = ws_url.rstrip("/")
        self.symbols = symbols

    def stream_url(self) -> str:
        streams = [f"{symbol.lower()}@kline_1m" for symbol in self.symbols]
        streams.extend(["!markPrice@arr@1s", "!forceOrder@arr"])
        return f"{self.ws_url}/stream?streams={'/'.join(streams)}"

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.stream_url(), ping_interval=20, ping_timeout=20) as ws:
                    log.info("binance_ws_connected")
                    backoff = 1
                    async for raw in ws:
                        yield json.loads(raw)
            except Exception as exc:
                log.warning("binance_ws_disconnected", error=str(exc), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def handle_ws_message(message: dict[str, Any], write: Callable[[str, dict[str, Any]], Any]) -> None:
    stream = message.get("stream", "")
    data = message.get("data", message)
    if "@kline_1m" in stream:
        row = closed_kline_from_ws(message)
        if row is not None:
            await write("kline", row)
        return
    if "markPrice" in stream:
        for item in data if isinstance(data, list) else [data]:
            await write("mark_price", mark_price_from_ws(item))
        return
    if "forceOrder" in stream:
        for item in data if isinstance(data, list) else [data]:
            await write("liquidation", liquidation_from_ws(item))


async def poll_rest_forever(
    rest: BinanceRestClient,
    symbols: list[str],
    session_factory: Any,
    interval: int,
    kline_backfill_limit: int = 180,
) -> None:
    funding_history_seeded = False
    while True:
        try:
            symbols_payload = await rest.exchange_info()
            async with session_factory() as session:
                async with session.begin():
                    await repository.upsert_symbols(session, symbols_payload)
            for symbol in symbols:
                try:
                    async with session_factory() as session:
                        async with session.begin():
                            await poll_symbol_public_context(
                                rest, session, symbol, kline_backfill_limit
                            )
                except Exception as exc:
                    log.warning(
                        "binance_rest_symbol_poll_failed",
                        symbol=symbol,
                        error=str(exc),
                    )
            if not funding_history_seeded:
                funding_history_seeded = await seed_funding_history_once(rest, symbols, session_factory)
            log.info("binance_rest_poll_complete", symbols=len(symbols))
        except Exception as exc:
            log.warning("binance_rest_poll_failed", error=str(exc))
        await asyncio.sleep(interval)


async def poll_symbol_public_context(
    rest: BinanceRestClient,
    session: Any,
    symbol: str,
    kline_backfill_limit: int,
) -> None:
    for row in await rest.klines_1m(symbol, limit=kline_backfill_limit):
        await repository.upsert_kline(session, row)
    mark_price = await rest.premium_index(symbol)
    await repository.upsert_mark_price(session, mark_price)
    open_interest = await rest.open_interest(symbol)
    open_interest["open_interest_value"] = open_interest["open_interest"] * mark_price["mark_price"]
    await repository.upsert_open_interest(session, open_interest)


async def seed_funding_history_once(
    rest: BinanceRestClient,
    symbols: list[str],
    session_factory: Any,
    limit: int = 100,
) -> bool:
    seeded = 0
    for symbol in symbols:
        async with session_factory() as session:
            try:
                async with session.begin():
                    for row in await rest.funding_rate_history(symbol, limit):
                        await repository.upsert_funding_history(session, row)
                seeded += 1
            except Exception as exc:
                log.warning("binance_funding_history_symbol_seed_failed", symbol=symbol, error=str(exc))
    if seeded == len(symbols):
        log.info("binance_funding_history_seeded", symbols=seeded, limit=limit)
        return True
    log.warning("binance_funding_history_seed_incomplete", seeded=seeded, symbols=len(symbols), limit=limit)
    return False
