from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from bn_monitor import repository

D0 = Decimal("0")
D1 = Decimal("1")
DEFAULT_NORMALIZED_MOVE_MIN_MAD = Decimal("0.0001")
LATEST_SYMBOL_ROWS_QUERY = """
                SELECT ts, symbol, open, high, low, close, quote_volume, taker_buy_quote_volume
                FROM (
                    SELECT DISTINCT ON (symbol)
                        ts, symbol, open, high, low, close, quote_volume, taker_buy_quote_volume
                    FROM futures_kline_1m
                    WHERE symbol IN :symbols
                      AND ts >= :stale_after
                    ORDER BY symbol, ts DESC
                ) latest
                ORDER BY symbol
                """


def latest_common_minute_query_shape() -> str:
    return LATEST_SYMBOL_ROWS_QUERY


@dataclass(frozen=True)
class KlinePoint:
    ts: datetime
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    quote_volume: Decimal
    taker_buy_quote_volume: Decimal


def pct_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None or previous == 0:
        return None
    return current / previous - D1


def robust_z(value: Decimal, sample: list[Decimal]) -> Decimal | None:
    if len(sample) < 5:
        return None
    med = median(sample)
    mad = median([abs(item - med) for item in sample])
    if mad == 0:
        return None
    return Decimal("0.6745") * (value - med) / mad


def rolling_mad(sample: list[Decimal]) -> Decimal | None:
    if len(sample) < 5:
        return None
    med = median(sample)
    mad = median([abs(item - med) for item in sample])
    return mad


def normalized_move(
    value: Decimal | None,
    sample: list[Decimal],
    absolute: bool = True,
    min_mad: Decimal | None = DEFAULT_NORMALIZED_MOVE_MIN_MAD,
) -> Decimal | None:
    if value is None:
        return None
    mad = rolling_mad(sample)
    if mad is None:
        return None
    if min_mad is not None and mad < min_mad:
        mad = min_mad
    numerator = abs(value) if absolute else value
    return numerator / mad


def percentile_rank(value: Decimal, sample: list[Decimal]) -> Decimal | None:
    if len(sample) < 5:
        return None
    lower_or_equal = sum(1 for item in sample if item <= value)
    return Decimal(lower_or_equal) / Decimal(len(sample))


def candle_body_ratio(open_: Decimal, high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
    candle_range = high - low
    if candle_range <= 0:
        return None
    return abs(close - open_) / candle_range


def candle_range_bps(high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
    if close <= 0:
        return None
    return (high - low) / close * Decimal("10000")


def taker_buy_ratio(taker_buy_quote_volume: Decimal, quote_volume: Decimal) -> Decimal | None:
    if quote_volume <= 0:
        return None
    return taker_buy_quote_volume / quote_volume


def build_indicator(
    current: KlinePoint,
    closes: dict[int, Decimal],
    volume_sample: list[Decimal],
    btc_return_1m: Decimal | None,
    market_median_return_1m: Decimal | None = None,
    oi_now: Decimal | None = None,
    oi_5m: Decimal | None = None,
    oi_15m: Decimal | None = None,
    oi_change_sample: list[Decimal] | None = None,
    funding_rate: Decimal | None = None,
    funding_percentile: Decimal | None = None,
    return_15m_sample: list[Decimal] | None = None,
    normalized_move_min_mad: Decimal = DEFAULT_NORMALIZED_MOVE_MIN_MAD,
) -> dict[str, Any]:
    return_1m = pct_change(current.close, closes.get(1))
    return_5m = pct_change(current.close, closes.get(5))
    return_15m = pct_change(current.close, closes.get(15))
    volume_z = robust_z(current.quote_volume, volume_sample)
    volume_pct = percentile_rank(current.quote_volume, volume_sample)
    buy_ratio = taker_buy_ratio(current.taker_buy_quote_volume, current.quote_volume)
    sell_ratio = D1 - buy_ratio if buy_ratio is not None else None
    oi_change_5m = pct_change(oi_now, oi_5m)
    oi_change_15m = pct_change(oi_now, oi_15m)
    oi_z = None
    if oi_change_15m is not None and oi_change_sample:
        oi_z = robust_z(oi_change_15m, oi_change_sample)
    price_move_norm = normalized_move(
        return_15m,
        return_15m_sample or [],
        absolute=True,
        min_mad=normalized_move_min_mad,
    )
    oi_move_norm = normalized_move(
        oi_change_15m,
        oi_change_sample or [],
        absolute=False,
        min_mad=normalized_move_min_mad,
    )
    btc_relative = return_1m - btc_return_1m if return_1m is not None and btc_return_1m is not None else None
    market_relative = (
        return_1m - market_median_return_1m
        if return_1m is not None and market_median_return_1m is not None
        else None
    )
    if current.symbol in {"BTCUSDT", "ETHUSDT"}:
        price_score = abs(market_relative) if market_relative is not None else None
    elif btc_relative is not None and market_relative is not None:
        price_score = min(abs(btc_relative), abs(market_relative))
    else:
        price_score = None
    flat_oi_score = oi_change_15m * Decimal("10000") if oi_change_15m is not None else None
    return {
        "ts": current.ts,
        "symbol": current.symbol,
        "return_1m": return_1m,
        "return_5m": return_5m,
        "return_15m": return_15m,
        "btc_relative_return_1m": btc_relative,
        "market_median_return_1m": market_median_return_1m,
        "market_relative_return_1m": market_relative,
        "beta_adjusted_return_1m": btc_relative,
        "quote_volume_1m": current.quote_volume,
        "volume_percentile": volume_pct,
        "volume_robust_z": volume_z,
        "taker_buy_ratio": buy_ratio,
        "taker_sell_ratio": sell_ratio,
        "candle_body_ratio": candle_body_ratio(current.open, current.high, current.low, current.close),
        "candle_range_bps": candle_range_bps(current.high, current.low, current.close),
        "oi_change_5m": oi_change_5m,
        "oi_change_15m": oi_change_15m,
        "oi_robust_z": oi_z,
        "funding_rate": funding_rate,
        "funding_percentile": funding_percentile,
        "price_move_norm_15m": price_move_norm,
        "oi_move_norm_15m": oi_move_norm,
        "price_spike_score": price_score,
        "flat_oi_buildup_score": flat_oi_score,
    }


async def compute_latest_indicators(
    session: AsyncSession,
    symbols: list[str],
    max_staleness_minutes: int = 5,
    normalized_move_min_mad_bps: float | Decimal = Decimal("1"),
) -> int:
    stale_after = datetime.now(UTC) - timedelta(minutes=max_staleness_minutes)
    rows = (
        await session.execute(
            text(
                LATEST_SYMBOL_ROWS_QUERY
            ).bindparams(bindparam("symbols", expanding=True)),
            {"symbols": symbols, "stale_after": stale_after},
        )
    ).mappings().all()
    if not rows:
        return 0

    latest_by_symbol = {
        row["symbol"]: KlinePoint(
            ts=row["ts"],
            symbol=row["symbol"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            quote_volume=row["quote_volume"],
            taker_buy_quote_volume=row["taker_buy_quote_volume"],
        )
        for row in rows
    }
    closes_by_symbol: dict[str, dict[int, Decimal]] = {}
    returns_by_symbol: dict[str, Decimal] = {}
    market_ts = (
        latest_by_symbol["BTCUSDT"].ts
        if "BTCUSDT" in latest_by_symbol
        else max(point.ts for point in latest_by_symbol.values())
    )
    btc_return = None
    eth_return = None
    for symbol, point in latest_by_symbol.items():
        closes = await _prior_closes(session, symbol, point.ts)
        closes_by_symbol[symbol] = closes
        return_1m = pct_change(point.close, closes.get(1))
        if return_1m is not None:
            returns_by_symbol[symbol] = return_1m
        if symbol == "BTCUSDT":
            btc_return = return_1m
        if symbol == "ETHUSDT":
            eth_return = return_1m

    market_median = median(returns_by_symbol.values()) if returns_by_symbol else None
    dispersion = (
        median([abs(item - market_median) for item in returns_by_symbol.values()])
        if market_median is not None
        else None
    )
    min_mad = Decimal(str(normalized_move_min_mad_bps)) / Decimal("10000")

    count = 0
    for symbol, point in latest_by_symbol.items():
        indicator = build_indicator(
            point,
            closes_by_symbol[symbol],
            await _volume_sample(session, symbol, point.ts),
            btc_return,
            market_median,
            *(await _oi_context(session, symbol, point.ts)),
            **await _funding_context(session, symbol, point.ts),
            return_15m_sample=await _return_15m_sample(session, symbol, point.ts),
            normalized_move_min_mad=min_mad,
        )
        await repository.upsert_indicator(session, indicator)
        count += 1

    if market_median is not None:
        await repository.upsert_market_factor(
            session,
            {
                "ts": market_ts,
                "btc_return_1m": btc_return,
                "eth_return_1m": eth_return,
                "market_median_return_1m": market_median,
                "market_dispersion_1m": dispersion,
                "created_at": datetime.now(UTC),
            },
        )
    return count


async def _prior_closes(session: AsyncSession, symbol: str, ts: datetime) -> dict[int, Decimal]:
    result = await session.execute(
        text(
            """
            SELECT 1 AS minutes, close FROM futures_kline_1m
            WHERE symbol=:symbol AND ts < :ts ORDER BY ts DESC LIMIT 1
            """
        ),
        {"symbol": symbol, "ts": ts},
    )
    one = result.mappings().first()
    out: dict[int, Decimal] = {}
    if one:
        out[1] = one["close"]
    for minutes in (5, 15):
        result = await session.execute(
            text(
                """
                SELECT close FROM futures_kline_1m
                WHERE symbol=:symbol AND ts <= :cutoff
                ORDER BY ts DESC LIMIT 1
                """
            ),
            {"symbol": symbol, "cutoff": ts - timedelta(minutes=minutes)},
        )
        row = result.mappings().first()
        if row:
            out[minutes] = row["close"]
    return out


async def _volume_sample(session: AsyncSession, symbol: str, ts: datetime) -> list[Decimal]:
    result = await session.execute(
        text(
            """
            SELECT quote_volume FROM futures_kline_1m
            WHERE symbol=:symbol AND ts < :ts AND ts >= :ts - interval '7 days'
            ORDER BY ts DESC LIMIT 10080
            """
        ),
        {"symbol": symbol, "ts": ts},
    )
    return [row[0] for row in result.all()]


async def _return_15m_sample(session: AsyncSession, symbol: str, ts: datetime) -> list[Decimal]:
    result = await session.execute(
        text(
            """
            SELECT close, lag(close, 15) OVER (ORDER BY ts) AS previous_close
            FROM futures_kline_1m
            WHERE symbol=:symbol AND ts < :ts AND ts >= :ts - interval '7 days'
            ORDER BY ts DESC
            LIMIT 10080
            """
        ),
        {"symbol": symbol, "ts": ts},
    )
    values: list[Decimal] = []
    for row in result.mappings().all():
        change = pct_change(row["close"], row["previous_close"])
        if change is not None:
            values.append(change)
    return values


def oi_change_sample(
    rows: list[dict[str, Any]], window_seconds: int = 900
) -> list[Decimal]:
    changes: list[Decimal] = []
    j = 1
    delta = timedelta(seconds=window_seconds)
    for i in range(len(rows)):
        if j <= i:
            j = i + 1
        target = rows[i]["ts"] - delta
        while j < len(rows) and rows[j]["ts"] > target:
            j += 1
        if j >= len(rows):
            break
        change = pct_change(rows[i]["open_interest"], rows[j]["open_interest"])
        if change is not None:
            changes.append(change)
    return changes


async def _oi_context(
    session: AsyncSession, symbol: str, ts: datetime
) -> tuple[Decimal | None, Decimal | None, Decimal | None, list[Decimal]]:
    result = await session.execute(
        text(
            """
            SELECT ts, open_interest FROM futures_open_interest
            WHERE symbol=:symbol AND ts <= :ts
            ORDER BY ts DESC LIMIT 240
            """
        ),
        {"symbol": symbol, "ts": ts},
    )
    rows = list(result.mappings().all())
    if not rows:
        return None, None, None, []
    now_oi = rows[0]["open_interest"]
    now_ts = rows[0]["ts"]
    oi_5m = next((row["open_interest"] for row in rows if (now_ts - row["ts"]).total_seconds() >= 300), None)
    oi_15m = next((row["open_interest"] for row in rows if (now_ts - row["ts"]).total_seconds() >= 900), None)
    return now_oi, oi_5m, oi_15m, oi_change_sample(rows)


async def _funding_context(session: AsyncSession, symbol: str, ts: datetime) -> dict[str, Decimal | None]:
    result = await session.execute(
        text(
            """
            SELECT funding_rate FROM futures_mark_price
            WHERE symbol=:symbol AND ts <= :ts
              AND funding_rate IS NOT NULL
              AND ts >= :ts - interval '7 days'
            ORDER BY ts DESC LIMIT 10080
            """
        ),
        {"symbol": symbol, "ts": ts},
    )
    rates = [row[0] for row in result.all()]
    latest = rates[0] if rates else None
    return {
        "funding_rate": latest,
        "funding_percentile": percentile_rank(latest, rates[1:]) if latest is not None else None,
    }
