from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bn_monitor.quality import FreshnessReport, KlineGapReport, staleness_minutes
from bn_monitor.reports import AlertSummary


def test_alert_summary_shape() -> None:
    summary = AlertSummary(
        symbol="BTCUSDT",
        alert_type="active_buy_impulse",
        severity="WARNING",
        mode="shadow",
        state="open",
        delivery_status="shadow",
        count=3,
    )
    assert summary.symbol == "BTCUSDT"
    assert summary.alert_type == "active_buy_impulse"
    assert summary.severity == "WARNING"
    assert summary.mode == "shadow"
    assert summary.count == 3


def test_kline_gap_report_includes_freshness_contract() -> None:
    report = KlineGapReport(
        symbol="BTCUSDT",
        first_ts="2026-01-01T00:00:00+00:00",
        last_ts="2026-01-01T00:59:00+00:00",
        expected_count=60,
        actual_count=60,
        gap_ratio=Decimal("0"),
        staleness_minutes=1,
        ok=True,
    )
    assert report.staleness_minutes == 1


def test_staleness_minutes_handles_naive_database_timestamp() -> None:
    now = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    last_ts = datetime(2026, 1, 1, 0, 7)
    assert staleness_minutes(last_ts, now) == 3


def test_freshness_report_shape() -> None:
    report = FreshnessReport(
        data_type="open_interest",
        symbol="BTCUSDT",
        last_ts="2026-01-01T00:00:00+00:00",
        staleness_minutes=1,
        ok=True,
    )
    assert report.data_type == "open_interest"
    assert report.ok
