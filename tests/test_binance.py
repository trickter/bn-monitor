from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from bn_monitor.binance import BinanceRestClient, BinanceStream, poll_symbol_public_context, select_universe_symbols
from bn_monitor.binance import closed_kline_from_ws, liquidation_from_ws


def test_only_closed_kline_is_persistable() -> None:
    message = {
        "stream": "btcusdt@kline_1m",
        "data": {
            "k": {
                "x": False,
                "T": 1710000060000,
                "s": "BTCUSDT",
                "o": "1",
                "h": "2",
                "l": "1",
                "c": "2",
                "v": "3",
                "q": "4",
                "n": 5,
                "V": "1",
                "Q": "2",
            }
        },
    }
    assert closed_kline_from_ws(message) is None
    message["data"]["k"]["x"] = True
    row = closed_kline_from_ws(message)
    assert row is not None
    assert row["symbol"] == "BTCUSDT"
    assert row["close"] == Decimal("2")


def test_liquidation_is_snapshot_quote_value() -> None:
    row = liquidation_from_ws(
        {
            "o": {
                "T": 1710000060000,
                "s": "BTCUSDT",
                "S": "SELL",
                "p": "100",
                "ap": "99",
                "q": "2",
            }
        }
    )
    assert row["quote_value"] == Decimal("198")
    assert row["raw"]["o"]["s"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_rest_client_retries_after_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, request=request)
        return httpx.Response(
            200,
            json={"symbol": "BTCUSDT", "openInterest": "10", "time": 1710000060000},
            request=request,
        )

    monkeypatch.setattr("bn_monitor.binance.asyncio.sleep", fake_sleep)
    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        row = await client.open_interest("BTCUSDT")
    finally:
        await client.close()

    assert row["open_interest"] == Decimal("10")
    assert calls == 2
    assert Decimal(str(sleeps[0])) == Decimal("0.25")


@pytest.mark.asyncio
async def test_open_interest_history_maps_rows() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/futures/data/openInterestHist"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["period"] == "5m"
        assert request.url.params["limit"] == "2"
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterest": "10.5",
                    "sumOpenInterestValue": "1050.25",
                    "timestamp": 1710000000000,
                }
            ],
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        rows = await client.open_interest_history("BTCUSDT", limit=2)
    finally:
        await client.close()

    assert rows[0]["ts"] == datetime.fromtimestamp(1710000000, tz=UTC)
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["open_interest"] == Decimal("10.5")
    assert rows[0]["open_interest_value"] == Decimal("1050.25")


@pytest.mark.asyncio
async def test_rest_client_premium_index_maps_mark_price_and_funding() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "markPrice": "100.5",
                "indexPrice": "100.0",
                "lastFundingRate": "0.0001",
                "nextFundingTime": 1710003600000,
                "time": 1710000060000,
            },
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        row = await client.premium_index("BTCUSDT")
    finally:
        await client.close()

    assert row["mark_price"] == Decimal("100.5")
    assert row["funding_rate"] == Decimal("0.0001")


@pytest.mark.asyncio
async def test_exchange_info_maps_symbol_filters() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            },
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        rows = await client.exchange_info()
    finally:
        await client.close()

    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["contract_type"] == "PERPETUAL"
    assert rows[0]["tick_size"] == Decimal("0.10")
    assert rows[0]["step_size"] == Decimal("0.001")
    assert rows[0]["min_notional"] == Decimal("5")


@pytest.mark.asyncio
async def test_exchange_info_filters_to_trading_usdt_perpetuals() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "filters": [],
                    },
                    {
                        "symbol": "BTCUSDC",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDC",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "filters": [],
                    },
                    {
                        "symbol": "ETHUSDT_260626",
                        "baseAsset": "ETH",
                        "quoteAsset": "USDT",
                        "contractType": "CURRENT_QUARTER",
                        "status": "TRADING",
                        "filters": [],
                    },
                    {
                        "symbol": "OLDUSDT",
                        "baseAsset": "OLD",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                        "status": "BREAK",
                        "filters": [],
                    },
                ]
            },
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        rows = await client.exchange_info()
    finally:
        await client.close()

    assert [row["symbol"] for row in rows] == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_rest_client_maps_24h_quote_volumes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/ticker/24hr"
        return httpx.Response(
            200,
            json=[
                {"symbol": "BTCUSDT", "quoteVolume": "5000000"},
                {"symbol": "ETHUSDT", "quoteVolume": "4999999.99"},
            ],
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        quote_volumes = await client.quote_volumes_24h()
    finally:
        await client.close()

    assert quote_volumes == {
        "BTCUSDT": Decimal("5000000"),
        "ETHUSDT": Decimal("4999999.99"),
    }


def test_ws_streams_use_market_route_and_chunk_klines() -> None:
    stream = BinanceStream("wss://fstream.binance.com", ["BTCUSDT", "ETHUSDT", "SOLUSDT"], 2)
    urls = stream.stream_urls()
    assert len(urls) == 3
    assert urls[0] == "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m/ethusdt@kline_1m"
    assert urls[1] == "wss://fstream.binance.com/market/stream?streams=solusdt@kline_1m"
    assert urls[2] == "wss://fstream.binance.com/market/stream?streams=!markPrice@arr@1s/!forceOrder@arr"


def test_select_universe_symbols_respects_mode_and_exclusions() -> None:
    rows = [
        {"symbol": "BTCUSDT", "is_active": True},
        {"symbol": "ETHUSDT", "is_active": True},
    ]
    assert select_universe_symbols(rows, ["SOLUSDT"], "configured", ["SOLUSDT"]) == []
    assert select_universe_symbols(
        rows,
        ["SOLUSDT"],
        "all_usdt_perpetual",
        ["ETHUSDT"],
        {"BTCUSDT": Decimal("5000000"), "ETHUSDT": Decimal("5000000")},
        5_000_000,
    ) == ["BTCUSDT"]


def test_select_universe_symbols_gates_all_mode_by_24h_quote_volume() -> None:
    rows = [
        {"symbol": "BTCUSDT", "is_active": True},
        {"symbol": "ETHUSDT", "is_active": True},
        {"symbol": "DOGEUSDT", "is_active": True},
    ]
    quote_volumes = {
        "BTCUSDT": Decimal("5000000"),
        "ETHUSDT": Decimal("4999999.99"),
    }

    assert select_universe_symbols(
        rows,
        ["DOGEUSDT"],
        "all_usdt_perpetual",
        [],
        quote_volumes,
        5_000_000,
    ) == ["BTCUSDT"]


def test_select_universe_symbols_configured_mode_ignores_24h_quote_volume_gate() -> None:
    rows = [
        {"symbol": "BTCUSDT", "is_active": True},
        {"symbol": "ETHUSDT", "is_active": True},
    ]
    quote_volumes = {
        "BTCUSDT": Decimal("1"),
    }

    assert select_universe_symbols(
        rows,
        ["BTCUSDT", "ETHUSDT"],
        "configured",
        [],
        quote_volumes,
        5_000_000,
    ) == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_funding_rate_history_maps_rows() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/fundingRate"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": 1710000000000,
                    "fundingRate": "0.0002",
                    "markPrice": "101.5",
                }
            ],
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        rows = await client.funding_rate_history("BTCUSDT", limit=1)
    finally:
        await client.close()

    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["funding_rate"] == Decimal("0.0002")
    assert rows[0]["mark_price"] == Decimal("101.5")


@pytest.mark.asyncio
async def test_rest_klines_maps_only_closed_rows() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        assert request.url.params["interval"] == "1m"
        return httpx.Response(
            200,
            json=[
                [
                    1709999940000,
                    "1",
                    "2",
                    "0.5",
                    "1.5",
                    "10",
                    1709999999999,
                    "15",
                    12,
                    "4",
                    "6",
                    "0",
                ],
                [
                    1710000000000,
                    "1",
                    "2",
                    "0.5",
                    "1.5",
                    "10",
                    1710000059999,
                    "15",
                    12,
                    "4",
                    "6",
                    "0",
                ],
            ],
            request=request,
        )

    client = BinanceRestClient(
        "https://example.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test"),
        min_interval_seconds=0,
    )
    try:
        rows = await client.klines_1m("BTCUSDT", limit=2, now_ms=1710000000000)
    finally:
        await client.close()

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["quote_volume"] == Decimal("15")
    assert rows[0]["taker_buy_quote_volume"] == Decimal("6")


@pytest.mark.asyncio
async def test_poll_symbol_public_context_prefers_open_interest_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[dict[str, object]] | dict[str, object]] = {}

    class Rest:
        current_called = False

        async def premium_index(self, symbol: str) -> dict[str, object]:
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "mark_price": Decimal("100"),
            }

        async def open_interest_history(
            self, symbol: str, period: str = "5m", limit: int = 30
        ) -> list[dict[str, object]]:
            assert period == "5m"
            assert limit == 1
            return [
                {
                    "ts": datetime.fromtimestamp(1710000000, tz=UTC),
                    "symbol": symbol,
                    "open_interest": Decimal("10"),
                    "open_interest_value": Decimal("1000"),
                }
            ]

        async def open_interest(self, symbol: str) -> dict[str, object]:
            self.current_called = True
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "open_interest": Decimal("11"),
                "open_interest_value": None,
            }

    async def fake_upsert_mark_price(session: object, row: dict[str, object]) -> None:
        captured["mark_price"] = row

    async def fake_upsert_open_interest(session: object, row: dict[str, object]) -> None:
        captured.setdefault("open_interest", []).append(row)

    monkeypatch.setattr("bn_monitor.binance.repository.upsert_mark_price", fake_upsert_mark_price)
    monkeypatch.setattr("bn_monitor.binance.repository.upsert_open_interest", fake_upsert_open_interest)
    rest = Rest()

    await poll_symbol_public_context(rest, object(), "BTCUSDT", 0)  # type: ignore[arg-type]

    assert rest.current_called is False
    assert captured["open_interest"] == [
        {
            "ts": datetime.fromtimestamp(1710000000, tz=UTC),
            "symbol": "BTCUSDT",
            "open_interest": Decimal("10"),
            "open_interest_value": Decimal("1000"),
        }
    ]


@pytest.mark.asyncio
async def test_poll_symbol_public_context_falls_back_when_open_interest_history_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[dict[str, object]] | dict[str, object]] = {}

    class Rest:
        async def premium_index(self, symbol: str) -> dict[str, object]:
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "mark_price": Decimal("100"),
            }

        async def open_interest_history(
            self, symbol: str, period: str = "5m", limit: int = 30
        ) -> list[dict[str, object]]:
            raise httpx.HTTPError("history unavailable")

        async def open_interest(self, symbol: str) -> dict[str, object]:
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "open_interest": Decimal("11"),
                "open_interest_value": None,
            }

    async def fake_upsert_mark_price(session: object, row: dict[str, object]) -> None:
        captured["mark_price"] = row

    async def fake_upsert_open_interest(session: object, row: dict[str, object]) -> None:
        captured.setdefault("open_interest", []).append(row)

    monkeypatch.setattr("bn_monitor.binance.repository.upsert_mark_price", fake_upsert_mark_price)
    monkeypatch.setattr("bn_monitor.binance.repository.upsert_open_interest", fake_upsert_open_interest)

    await poll_symbol_public_context(Rest(), object(), "BTCUSDT", 0)  # type: ignore[arg-type]

    assert captured["open_interest"] == [
        {
            "ts": datetime.fromtimestamp(1710000060, tz=UTC),
            "symbol": "BTCUSDT",
            "open_interest": Decimal("11"),
            "open_interest_value": Decimal("1100"),
        }
    ]


@pytest.mark.asyncio
async def test_poll_symbol_public_context_can_skip_open_interest_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict[str, object]] = {}

    class Rest:
        async def premium_index(self, symbol: str) -> dict[str, object]:
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "mark_price": Decimal("100"),
            }

        async def open_interest_history(
            self, symbol: str, period: str = "5m", limit: int = 30
        ) -> list[dict[str, object]]:
            raise AssertionError("OI history should be skipped")

    async def fake_upsert_mark_price(session: object, row: dict[str, object]) -> None:
        captured["mark_price"] = row

    async def fake_upsert_open_interest(session: object, row: dict[str, object]) -> None:
        raise AssertionError("OI upsert should be skipped")

    monkeypatch.setattr("bn_monitor.binance.repository.upsert_mark_price", fake_upsert_mark_price)
    monkeypatch.setattr("bn_monitor.binance.repository.upsert_open_interest", fake_upsert_open_interest)

    await poll_symbol_public_context(
        Rest(),
        object(),
        "BTCUSDT",
        0,
        poll_open_interest=False,
    )  # type: ignore[arg-type]

    assert captured["mark_price"]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_poll_symbol_public_context_falls_back_when_open_interest_history_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list[dict[str, object]] | dict[str, object]] = {}

    class Rest:
        async def premium_index(self, symbol: str) -> dict[str, object]:
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "mark_price": Decimal("100"),
            }

        async def open_interest_history(
            self, symbol: str, period: str = "5m", limit: int = 30
        ) -> list[dict[str, object]]:
            return []

        async def open_interest(self, symbol: str) -> dict[str, object]:
            return {
                "ts": datetime.fromtimestamp(1710000060, tz=UTC),
                "symbol": symbol,
                "open_interest": Decimal("12"),
                "open_interest_value": None,
            }

    async def fake_upsert_mark_price(session: object, row: dict[str, object]) -> None:
        captured["mark_price"] = row

    async def fake_upsert_open_interest(session: object, row: dict[str, object]) -> None:
        captured.setdefault("open_interest", []).append(row)

    monkeypatch.setattr("bn_monitor.binance.repository.upsert_mark_price", fake_upsert_mark_price)
    monkeypatch.setattr("bn_monitor.binance.repository.upsert_open_interest", fake_upsert_open_interest)

    await poll_symbol_public_context(Rest(), object(), "BTCUSDT", 0)  # type: ignore[arg-type]

    assert captured["open_interest"] == [
        {
            "ts": datetime.fromtimestamp(1710000060, tz=UTC),
            "symbol": "BTCUSDT",
            "open_interest": Decimal("12"),
            "open_interest_value": Decimal("1200"),
        }
    ]
