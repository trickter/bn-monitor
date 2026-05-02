from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class KlineGapReport:
    symbol: str
    first_ts: str | None
    last_ts: str | None
    expected_count: int
    actual_count: int
    gap_ratio: Decimal
    staleness_minutes: int | None
    ok: bool


@dataclass(frozen=True)
class FreshnessReport:
    data_type: str
    symbol: str
    last_ts: str | None
    staleness_minutes: int | None
    ok: bool


def calculate_gap_ratio(expected_count: int, actual_count: int) -> Decimal:
    if expected_count <= 0:
        return Decimal("0")
    missing = max(expected_count - actual_count, 0)
    return Decimal(missing) / Decimal(expected_count)


def expected_minutes_for_lookback(lookback_hours: int) -> int:
    return max(lookback_hours * 60, 0)


def staleness_minutes(last_ts: datetime | None, now: datetime | None = None) -> int | None:
    if last_ts is None:
        return None
    reference = now or datetime.now(UTC)
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=UTC)
    return max(int((reference - last_ts).total_seconds() // 60), 0)


async def kline_gap_reports(
    session: AsyncSession,
    symbols: list[str],
    max_gap_ratio: float,
    lookback_hours: int = 24,
    max_staleness_minutes: int = 3,
) -> list[KlineGapReport]:
    db_rows = (
        await session.execute(
            text(
                """
                WITH per_symbol AS (
                    SELECT
                        symbol,
                        min(ts) AS first_ts,
                        max(ts) AS last_ts,
                        count(*) AS actual_count
                    FROM futures_kline_1m
                    WHERE symbol IN :symbols
                      AND ts >= now() - (:lookback_hours || ' hours')::interval
                    GROUP BY symbol
                )
                SELECT
                    symbol,
                    first_ts,
                    last_ts,
                    actual_count
                FROM per_symbol
                ORDER BY symbol
                """
            ).bindparams(bindparam("symbols", expanding=True)),
            {"symbols": symbols, "lookback_hours": lookback_hours},
        )
    ).mappings().all()
    rows = {row["symbol"]: row for row in db_rows}
    reports: list[KlineGapReport] = []
    max_ratio = Decimal(str(max_gap_ratio))
    expected_count = expected_minutes_for_lookback(lookback_hours)
    now = datetime.now(UTC)
    for symbol in sorted(symbols):
        row = rows.get(symbol)
        if row is None:
            reports.append(
                KlineGapReport(
                    symbol=symbol,
                    first_ts=None,
                    last_ts=None,
                    expected_count=expected_count,
                    actual_count=0,
                    gap_ratio=Decimal("1"),
                    staleness_minutes=None,
                    ok=False,
                )
            )
            continue
        actual = int(row["actual_count"])
        ratio = calculate_gap_ratio(expected_count, actual)
        stale_minutes = staleness_minutes(row["last_ts"], now)
        reports.append(
            KlineGapReport(
                symbol=symbol,
                first_ts=row["first_ts"].isoformat() if row["first_ts"] else None,
                last_ts=row["last_ts"].isoformat() if row["last_ts"] else None,
                expected_count=expected_count,
                actual_count=actual,
                gap_ratio=ratio,
                staleness_minutes=stale_minutes,
                ok=ratio <= max_ratio
                and stale_minutes is not None
                and stale_minutes <= max_staleness_minutes,
            )
        )
    return reports


async def table_freshness_reports(
    session: AsyncSession,
    symbols: list[str],
    data_type: str,
    max_staleness_minutes: int,
) -> list[FreshnessReport]:
    table_by_type = {
        "open_interest": "futures_open_interest",
        "mark_price": "futures_mark_price",
    }
    table = table_by_type[data_type]
    db_rows = (
        await session.execute(
            text(
                f"""
                SELECT symbol, max(ts) AS last_ts
                FROM {table}
                WHERE symbol IN :symbols
                GROUP BY symbol
                ORDER BY symbol
                """
            ).bindparams(bindparam("symbols", expanding=True)),
            {"symbols": symbols},
        )
    ).mappings().all()
    rows = {row["symbol"]: row for row in db_rows}
    now = datetime.now(UTC)
    reports: list[FreshnessReport] = []
    for symbol in sorted(symbols):
        row = rows.get(symbol)
        if row is None:
            reports.append(
                FreshnessReport(
                    data_type=data_type,
                    symbol=symbol,
                    last_ts=None,
                    staleness_minutes=None,
                    ok=False,
                )
            )
            continue
        stale_minutes = staleness_minutes(row["last_ts"], now)
        reports.append(
            FreshnessReport(
                data_type=data_type,
                symbol=symbol,
                last_ts=row["last_ts"].isoformat() if row["last_ts"] else None,
                staleness_minutes=stale_minutes,
                ok=stale_minutes is not None and stale_minutes <= max_staleness_minutes,
            )
        )
    return reports
