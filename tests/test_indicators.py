from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bn_monitor.indicators import (
    KlinePoint,
    build_indicator,
    latest_common_minute_query_shape,
    normalized_move,
    oi_change_sample,
    percentile_rank,
    robust_z,
)
from bn_monitor.quality import KlineGapReport, calculate_gap_ratio, expected_minutes_for_lookback


def test_robust_baseline_and_indicator_snapshot() -> None:
    sample = [Decimal(i) for i in range(1, 11)]
    assert percentile_rank(Decimal("10"), sample) == Decimal("1")
    assert robust_z(Decimal("10"), sample) is not None

    indicator = build_indicator(
        KlinePoint(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            symbol="SOLUSDT",
            open=Decimal("100"),
            high=Decimal("103"),
            low=Decimal("99"),
            close=Decimal("102"),
            quote_volume=Decimal("1000"),
            taker_buy_quote_volume=Decimal("800"),
        ),
        {1: Decimal("100"), 5: Decimal("98"), 15: Decimal("96")},
        [Decimal(i) for i in range(100, 1000, 100)],
        Decimal("0.005"),
        Decimal("0.004"),
        oi_now=Decimal("110"),
        oi_5m=Decimal("100"),
        oi_15m=Decimal("90"),
        oi_change_sample=[Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.04"), Decimal("0.05")],
        funding_rate=Decimal("0.0001"),
        funding_percentile=Decimal("0.8"),
        return_15m_sample=[
            Decimal("-0.04"),
            Decimal("-0.02"),
            Decimal("-0.01"),
            Decimal("0.01"),
            Decimal("0.02"),
            Decimal("0.04"),
        ],
    )
    assert indicator["return_1m"] == Decimal("0.02")
    assert indicator["btc_relative_return_1m"] == Decimal("0.015")
    assert indicator["market_relative_return_1m"] == Decimal("0.016")
    assert indicator["price_spike_score"] == Decimal("0.015")
    assert indicator["taker_buy_ratio"] == Decimal("0.8")
    assert indicator["taker_sell_ratio"] == Decimal("0.2")
    assert indicator["funding_percentile"] == Decimal("0.8")
    assert indicator["price_move_norm_15m"] is not None
    assert indicator["oi_move_norm_15m"] is not None


def test_kline_gap_ratio_calculation() -> None:
    assert calculate_gap_ratio(1000, 999) == Decimal("0.001")
    assert calculate_gap_ratio(0, 0) == Decimal("0")
    assert expected_minutes_for_lookback(24) == 1440
    assert normalized_move(Decimal("0.02"), [Decimal("-0.02"), Decimal("-0.01"), Decimal("0"), Decimal("0.01"), Decimal("0.02")]) == Decimal("2")
    assert normalized_move(
        Decimal("0.02"),
        [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0.00001")],
        min_mad=Decimal("0.01"),
    ) == Decimal("2")


def test_indicator_query_uses_per_symbol_latest_minute() -> None:
    query = latest_common_minute_query_shape()
    assert "DISTINCT ON (symbol)" in query
    assert "AND ts >= :stale_after" in query
    assert "HAVING count(DISTINCT symbol) = :symbol_count" not in query


def test_missing_symbol_gap_report_shape() -> None:
    report = KlineGapReport("NEWUSDT", None, None, 1440, 0, Decimal("1"), None, False)
    assert not report.ok
    assert report.gap_ratio == Decimal("1")
    assert report.staleness_minutes is None


def test_oi_change_sample_pairs_by_time_window() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {"ts": base - timedelta(seconds=offset), "open_interest": Decimal(value)}
        for offset, value in [
            (0, 110),
            (60, 108),
            (300, 105),
            (600, 102),
            (900, 100),
            (1200, 99),
            (1800, 95),
        ]
    ]
    sample = oi_change_sample(rows, window_seconds=900)
    assert sample == [
        Decimal("110") / Decimal("100") - 1,
        Decimal("108") / Decimal("99") - 1,
        Decimal("105") / Decimal("99") - 1,
        Decimal("102") / Decimal("95") - 1,
        Decimal("100") / Decimal("95") - 1,
    ]


def test_oi_change_sample_handles_uneven_cadence() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {"ts": base - timedelta(seconds=offset), "open_interest": Decimal(value)}
        for offset, value in [(0, 100), (450, 98), (1500, 90)]
    ]
    sample = oi_change_sample(rows, window_seconds=900)
    assert sample == [
        Decimal("100") / Decimal("90") - 1,
        Decimal("98") / Decimal("90") - 1,
    ]


def test_oi_change_sample_empty_when_no_old_enough_row() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {"ts": base - timedelta(seconds=offset), "open_interest": Decimal(value)}
        for offset, value in [(0, 100), (60, 99), (120, 98)]
    ]
    assert oi_change_sample(rows, window_seconds=900) == []
