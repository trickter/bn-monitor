from __future__ import annotations

import asyncio
import json
import os
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
            if not _is_usdt_perpetual_trading_symbol(item):
                continue
            filters = {flt["filterType"]: flt for flt in item.get("filters", [])}
            rows.append(
                {
                    "exchange": "binance",
                    "market_type": "usdm_futures",
                    "symbol": item["symbol"],
                    "base_asset": item["baseAsset"],
                    "quote_asset": item["quoteAsset"],
                    "contract_type": item.get("contractType"),
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

    async def quote_volumes_24h(self) -> dict[str, Decimal]:
        response = await self._get("/fapi/v1/ticker/24hr")
        return {
            item["symbol"].upper(): Decimal(item["quoteVolume"])
            for item in response.json()
            if "symbol" in item and "quoteVolume" in item
        }

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        response = await self._get("/fapi/v1/openInterest", params={"symbol": symbol})
        payload = response.json()
        return {
            "ts": ms_to_datetime(payload["time"]),
            "symbol": payload["symbol"].upper(),
            "open_interest": Decimal(payload["openInterest"]),
            "open_interest_value": None,
        }

    async def open_interest_history(
        self, symbol: str, period: str = "5m", limit: int = 30
    ) -> list[dict[str, Any]]:
        response = await self._get(
            "/futures/data/openInterestHist",
            params={"symbol": symbol, "period": period, "limit": limit},
        )
        return [
            {
                "ts": ms_to_datetime(item["timestamp"]),
                "symbol": item.get("symbol", symbol).upper(),
                "open_interest": Decimal(item["sumOpenInterest"]),
                "open_interest_value": Decimal(item["sumOpenInterestValue"])
                if item.get("sumOpenInterestValue") is not None
                else None,
            }
            for item in response.json()
        ]

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
    def __init__(self, ws_url: str, symbols: list[str], kline_chunk_size: int = 300) -> None:
        self.ws_url = ws_url.rstrip("/")
        self.symbols = symbols
        self.kline_chunk_size = kline_chunk_size

    def stream_url(self) -> str:
        return self.stream_urls()[0]

    def stream_urls(self) -> list[str]:
        urls = []
        for chunk in _chunks(self.symbols, self.kline_chunk_size):
            streams = [f"{symbol.lower()}@kline_1m" for symbol in chunk]
            urls.append(_combined_market_stream_url(self.ws_url, streams))
        urls.append(_combined_market_stream_url(self.ws_url, ["!markPrice@arr@1s", "!forceOrder@arr"]))
        return urls

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        async for message in self.messages_from_url(self.stream_url()):
            yield message

    async def messages_from_url(self, url: str) -> AsyncIterator[dict[str, Any]]:
        backoff = 1
        while True:
            try:
                proxy = os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, proxy=proxy or None) as ws:
                    log.info("binance_ws_connected", url=url)
                    backoff = 1
                    async for raw in ws:
                        yield json.loads(raw)
            except Exception as exc:
                log.warning("binance_ws_disconnected", error=str(exc), backoff=backoff, url=url)
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
    open_interest_poll_interval_seconds: int = 300,
) -> None:
    funding_history_seeded = False
    kline_backfill_seeded = False
    last_open_interest_poll_at: float | None = None
    while True:
        loop_time = asyncio.get_running_loop().time()
        poll_open_interest = (
            last_open_interest_poll_at is None
            or loop_time - last_open_interest_poll_at >= open_interest_poll_interval_seconds
        )
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
                                rest,
                                session,
                                symbol,
                                kline_backfill_limit if not kline_backfill_seeded else 0,
                                poll_open_interest=poll_open_interest,
                            )
                except Exception as exc:
                    log.warning(
                        "binance_rest_symbol_poll_failed",
                        symbol=symbol,
                        error=str(exc),
                    )
            if not funding_history_seeded:
                funding_history_seeded = await seed_funding_history_once(rest, symbols, session_factory)
            kline_backfill_seeded = True
            if poll_open_interest:
                last_open_interest_poll_at = loop_time
            log.info("binance_rest_poll_complete", symbols=len(symbols))
        except Exception as exc:
            log.warning("binance_rest_poll_failed", error=str(exc))
        await asyncio.sleep(interval)


async def poll_symbol_public_context(
    rest: BinanceRestClient,
    session: Any,
    symbol: str,
    kline_backfill_limit: int,
    poll_open_interest: bool = True,
) -> None:
    if kline_backfill_limit > 0:
        for row in await rest.klines_1m(symbol, limit=kline_backfill_limit):
            await repository.upsert_kline(session, row)
    mark_price = await rest.premium_index(symbol)
    await repository.upsert_mark_price(session, mark_price)
    if not poll_open_interest:
        return
    # Historical rows carry open_interest_value; fallback derives it from mark price.
    open_interest_rows = await _open_interest_rows_with_fallback(rest, symbol, mark_price)
    for open_interest in open_interest_rows:
        await repository.upsert_open_interest(session, open_interest)


async def _open_interest_rows_with_fallback(
    rest: BinanceRestClient,
    symbol: str,
    mark_price: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        open_interest_history = await rest.open_interest_history(symbol, period="5m", limit=1)
    except Exception as exc:
        log.warning("binance_open_interest_history_failed", symbol=symbol, error=str(exc))
        open_interest_history = []
    if open_interest_history:
        return open_interest_history
    open_interest = await rest.open_interest(symbol)
    open_interest["open_interest_value"] = open_interest["open_interest"] * mark_price["mark_price"]
    return [open_interest]


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


def _is_usdt_perpetual_trading_symbol(item: dict[str, Any]) -> bool:
    return (
        item.get("quoteAsset") == "USDT"
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
    )


def _chunks(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [items[index : index + size] for index in range(0, len(items), size)]


def _combined_market_stream_url(ws_url: str, streams: list[str]) -> str:
    return f"{ws_url.rstrip('/')}/market/stream?streams={'/'.join(streams)}"


def select_universe_symbols(
    rows: list[dict[str, Any]],
    configured: list[str],
    mode: str,
    excluded: list[str],
    quote_volumes_24h: dict[str, Decimal] | None = None,
    min_24h_quote_volume_usd: int | Decimal = 0,
) -> list[str]:
    excluded_set = {symbol.upper() for symbol in excluded}
    if mode == "all_usdt_perpetual":
        min_quote_volume = Decimal(str(min_24h_quote_volume_usd))
        quote_volumes = quote_volumes_24h or {}
        symbols = [
            symbol
            for row in rows
            if row.get("is_active")
            for symbol in [row["symbol"].upper()]
            if quote_volumes.get(symbol, Decimal("0")) >= min_quote_volume
        ]
    else:
        symbols = [symbol.upper() for symbol in configured]
    return [symbol for symbol in symbols if symbol not in excluded_set]
