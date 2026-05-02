from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bn_monitor.alerts import (
    AlertDecision,
    AlertService,
    ALERT_EXPIRE_AFTER,
    INDICATOR_ALERT_TYPES,
    aggregate_symbol_alerts,
    build_market_digest,
    cooldown_window,
    generate_alerts,
    generate_liquidation_snapshot_confirmation,
    prepare_indicator_decisions_for_mode,
    prioritize_alerts,
)
from bn_monitor.config import Settings
from bn_monitor.discord import discord_payload


def test_shadow_and_live_modes_are_configured() -> None:
    assert Settings(alert_mode="shadow").alert_mode == "shadow"
    assert Settings(alert_mode="live").alert_mode == "live"
    assert "active_buy_impulse" in INDICATOR_ALERT_TYPES
    assert ALERT_EXPIRE_AFTER == timedelta(hours=6)


def test_active_buy_alert_has_required_payload_and_embed_fields() -> None:
    settings = Settings(alert_mode="shadow")
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "SOLUSDT",
            "btc_relative_return_1m": Decimal("0.006"),
            "market_relative_return_1m": Decimal("0.005"),
            "btc_return_1m": Decimal("0.001"),
            "market_median_return_1m": Decimal("0.002"),
            "volume_percentile": Decimal("0.995"),
            "volume_robust_z": Decimal("5"),
            "taker_buy_ratio": Decimal("0.8"),
            "candle_body_ratio": Decimal("0.8"),
            "candle_range_bps": Decimal("80"),
            "oi_robust_z": Decimal("3"),
            "oi_change_15m": Decimal("0.03"),
            "oi_move_norm_15m": Decimal("3"),
            "price_move_norm_15m": Decimal("1.5"),
            "funding_rate": Decimal("0.0001"),
            "funding_percentile": Decimal("0.9"),
            "return_15m": Decimal("0.01"),
        },
        settings,
    )
    assert alerts[0].alert_type == "active_buy_impulse"
    assert alerts[0].severity == "CRITICAL"
    payload = discord_payload(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "SOLUSDT",
            "direction": alerts[0].direction,
            "severity": alerts[0].severity,
            "title": alerts[0].title,
            "message": alerts[0].message,
            "payload": alerts[0].payload,
        },
        123,
    )
    field_names = {field["name"] for field in payload["embeds"][0]["fields"]}
    assert {
        "交易对 (symbol)",
        "方向 (direction)",
        "级别 (severity)",
        "触发条件 (conditions)",
        "核心指标 (metrics)",
        "市场背景 (context)",
        "Payload ID",
    } <= field_names
    market_context = next(field["value"] for field in payload["embeds"][0]["fields"] if field["name"] == "市场背景 (context)")
    core_metrics = next(field["value"] for field in payload["embeds"][0]["fields"] if field["name"] == "核心指标 (metrics)")
    assert "BTC 1m 收益" in market_context
    assert "资金费率" in core_metrics
    assert "资金费率分位" in core_metrics
    assert "p90.0" in core_metrics
    assert alerts[0].payload["funding_percentile"] == "0.9"


def test_warmup_indicator_without_baseline_does_not_alert() -> None:
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "NEWUSDT",
            "btc_relative_return_1m": Decimal("0.10"),
            "market_relative_return_1m": None,
            "volume_percentile": None,
            "volume_robust_z": None,
            "taker_buy_ratio": Decimal("0.95"),
            "candle_body_ratio": Decimal("0.9"),
            "candle_range_bps": Decimal("100"),
            "oi_robust_z": None,
            "oi_change_15m": None,
            "oi_move_norm_15m": None,
            "price_move_norm_15m": None,
            "funding_rate": None,
            "funding_percentile": None,
            "return_15m": Decimal("0.10"),
        },
        Settings(alert_mode="live"),
    )
    assert alerts == []


def test_sideways_absorption_requires_percentile_and_robust_z_confirmation() -> None:
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "AVAXUSDT",
            "btc_relative_return_1m": Decimal("0.000001"),
            "market_relative_return_1m": Decimal("0"),
            "btc_return_1m": Decimal("0.0001"),
            "market_median_return_1m": Decimal("0.0001"),
            "volume_percentile": Decimal("0.928"),
            "volume_robust_z": Decimal("6.15"),
            "taker_buy_ratio": Decimal("0.017"),
            "candle_body_ratio": Decimal("0.1"),
            "candle_range_bps": Decimal("20"),
            "oi_robust_z": Decimal("-0.24"),
            "oi_change_15m": Decimal("0.0002"),
            "oi_move_norm_15m": Decimal("0.43"),
            "price_move_norm_15m": Decimal("0.2"),
            "funding_rate": Decimal("-0.000056"),
            "funding_percentile": Decimal("0.996"),
        },
        Settings(alert_mode="live"),
    )
    assert alerts == []


def test_absorption_triggers_only_after_strict_volume_confirmation() -> None:
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "AVAXUSDT",
            "btc_relative_return_1m": Decimal("0.000001"),
            "market_relative_return_1m": Decimal("0"),
            "volume_percentile": Decimal("0.995"),
            "volume_robust_z": Decimal("6.5"),
            "taker_buy_ratio": Decimal("0.05"),
            "candle_body_ratio": Decimal("0.1"),
            "candle_range_bps": Decimal("20"),
            "oi_robust_z": Decimal("0"),
            "oi_change_15m": Decimal("0"),
            "oi_move_norm_15m": Decimal("0"),
            "price_move_norm_15m": Decimal("0.2"),
        },
        Settings(alert_mode="live"),
    )
    assert [alert.alert_type for alert in alerts] == ["sell_absorption"]


def test_cooldown_allows_critical_every_five_minutes() -> None:
    service = AlertService(Settings(alert_mode="live"))
    decision = AlertDecision("x", "CRITICAL", "up", Decimal("1"), "title", "message", {"symbol": "BTCUSDT"})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert service.should_send(decision, now)
    assert not service.should_send(decision, now + timedelta(minutes=4))
    assert service.should_send(decision, now + timedelta(minutes=5, seconds=1))
    assert cooldown_window("CRITICAL") == timedelta(minutes=5)
    assert cooldown_window("WARNING") == timedelta(minutes=10)
    assert cooldown_window(decision, Settings(alert_cooldown_minutes={"x": 30})) == timedelta(minutes=30)
    assert cooldown_window(
        AlertDecision(
            "sell_absorption",
            "WARNING",
            "neutral",
            Decimal("1"),
            "title",
            "message",
            {"symbol": "AVAXUSDT"},
        ),
        Settings(),
    ) == timedelta(minutes=60)


def test_record_cooldown_method_updates_count_1h_contract() -> None:
    import inspect

    source = inspect.getsource(AlertService.record_cooldown)
    assert "timedelta(hours=1)" in source
    assert "count_1h" in source


def test_market_digest_triggers_from_many_symbols() -> None:
    decisions = [
        AlertDecision(
            "x",
            "WARNING",
            "up",
            Decimal(i),
            "title",
            "message",
            {"symbol": f"S{i}", "ts": "2026-01-01T00:00:00+00:00"},
        )
        for i in range(6)
    ]
    digest = build_market_digest(decisions, trigger_count=5)
    assert digest is not None
    assert digest.alert_type == "market_digest"
    assert len(digest.payload["top_movers"]) == 6


def test_live_market_digest_replaces_many_individual_deliveries() -> None:
    decisions = [
        AlertDecision(
            "x",
            "CRITICAL",
            "up",
            Decimal(i),
            "title",
            "message",
            {"symbol": f"S{i}", "ts": "2026-01-01T00:00:00+00:00"},
        )
        for i in range(5)
    ]
    digest = build_market_digest(decisions, Settings().digest_trigger_count)
    assert digest is not None
    assert digest.severity == "CRITICAL"
    assert digest.payload["symbol"] == "MARKET"


def test_market_digest_requires_same_minute() -> None:
    decisions = [
        AlertDecision(
            "x",
            "WARNING",
            "up",
            Decimal(i),
            "title",
            "message",
            {"symbol": f"S{i}", "ts": f"2026-01-01T00:0{i}:00+00:00"},
        )
        for i in range(5)
    ]
    assert build_market_digest(decisions, trigger_count=5) is None


def test_market_digest_prefers_latest_eligible_minute() -> None:
    older = [
        AlertDecision(
            "old",
            "WARNING",
            "up",
            Decimal(i),
            "title",
            "message",
            {"symbol": f"O{i}", "ts": "2026-01-01T00:00:00+00:00"},
        )
        for i in range(6)
    ]
    newer = [
        AlertDecision(
            "new",
            "WARNING",
            "up",
            Decimal(i),
            "title",
            "message",
            {"symbol": f"N{i}", "ts": "2026-01-01T00:01:00+00:00"},
        )
        for i in range(5)
    ]
    digest = build_market_digest(older + newer, trigger_count=5)
    assert digest is not None
    assert all(item["symbol"].startswith("N") for item in digest.payload["top_movers"])


def test_liquidation_snapshot_confirmation_uses_snapshot_language() -> None:
    decision = generate_liquidation_snapshot_confirmation(
        symbol="BTCUSDT",
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        side="SELL",
        quote_value=Decimal("1000"),
        quote_sample=[Decimal(i) for i in range(1, 100)],
        indicator={"btc_relative_return_1m": Decimal("-0.01"), "oi_change_15m": Decimal("-0.02")},
    )
    assert decision is not None
    assert decision.alert_type == "liquidation_snapshot_confirmation"
    assert "snapshot" in decision.title
    assert "liquidation_snapshot_quote_1m_percentile" in decision.payload


def test_flat_oi_buildup_uses_normalized_price_and_oi_moves() -> None:
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "LINKUSDT",
            "btc_relative_return_1m": Decimal("0"),
            "market_relative_return_1m": Decimal("0"),
            "volume_percentile": Decimal("0.80"),
            "volume_robust_z": Decimal("1"),
            "taker_buy_ratio": Decimal("0.55"),
            "candle_body_ratio": Decimal("0.5"),
            "candle_range_bps": Decimal("20"),
            "oi_robust_z": Decimal("1"),
            "oi_move_norm_15m": Decimal("2.5"),
            "oi_change_15m": Decimal("0.02"),
            "price_move_norm_15m": Decimal("0.4"),
            "return_15m": Decimal("0.02"),
        },
        Settings(alert_mode="shadow"),
    )
    assert alerts == []


def test_flat_oi_buildup_requires_p90_volume_and_min_oi_change() -> None:
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "LINKUSDT",
            "btc_relative_return_1m": Decimal("0"),
            "market_relative_return_1m": Decimal("0"),
            "volume_percentile": Decimal("0.95"),
            "volume_robust_z": Decimal("1"),
            "taker_buy_ratio": Decimal("0.55"),
            "candle_body_ratio": Decimal("0.5"),
            "candle_range_bps": Decimal("20"),
            "oi_robust_z": Decimal("1"),
            "oi_move_norm_15m": Decimal("2.5"),
            "oi_change_15m": Decimal("0.02"),
            "price_move_norm_15m": Decimal("0.4"),
            "flat_oi_buildup_score": Decimal("200"),
            "return_15m": Decimal("0.02"),
        },
        Settings(alert_mode="shadow"),
    )
    assert [alert.alert_type for alert in alerts] == ["flat_oi_buildup"]
    assert alerts[0].payload["price_move_norm_15m"] == "0.4"
    assert alerts[0].payload["oi_change_bps_15m"] == "200.00"


def test_alt_directional_alert_requires_btc_and_market_relative() -> None:
    base = {
        "ts": datetime(2026, 1, 1, tzinfo=UTC),
        "symbol": "SOLUSDT",
        "btc_relative_return_1m": Decimal("0.006"),
        "volume_percentile": Decimal("0.995"),
        "volume_robust_z": Decimal("5"),
        "taker_buy_ratio": Decimal("0.8"),
        "candle_body_ratio": Decimal("0.8"),
        "candle_range_bps": Decimal("80"),
        "oi_robust_z": Decimal("1"),
    }
    assert generate_alerts({**base, "market_relative_return_1m": None}, Settings()) == []
    assert generate_alerts({**base, "market_relative_return_1m": Decimal("0.005")}, Settings())


def test_btc_directional_alert_uses_market_relative_path() -> None:
    alerts = generate_alerts(
        {
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "symbol": "BTCUSDT",
            "btc_relative_return_1m": Decimal("0"),
            "market_relative_return_1m": Decimal("0.006"),
            "volume_percentile": Decimal("0.995"),
            "volume_robust_z": Decimal("5"),
            "taker_buy_ratio": Decimal("0.8"),
            "candle_body_ratio": Decimal("0.8"),
            "candle_range_bps": Decimal("80"),
            "oi_robust_z": Decimal("1"),
        },
        Settings(),
    )
    assert [alert.alert_type for alert in alerts] == ["active_buy_impulse"]


def test_critical_alerts_are_prioritized_before_warnings() -> None:
    warning = AlertDecision("warning", "WARNING", "up", Decimal("100"), "title", "message", {"symbol": "A"})
    critical = AlertDecision("critical", "CRITICAL", "up", Decimal("1"), "title", "message", {"symbol": "B"})
    assert prioritize_alerts([warning, critical]) == [critical, warning]


def test_same_symbol_alert_types_are_aggregated_within_30_seconds() -> None:
    first = AlertDecision(
        "buy_absorption",
        "WARNING",
        "neutral",
        Decimal("2"),
        "title",
        "message",
        {"symbol": "SOLUSDT", "ts": "2026-01-01T00:00:10+00:00", "trigger_conditions": ["a"]},
    )
    second = AlertDecision(
        "flat_oi_buildup",
        "CRITICAL",
        "neutral",
        Decimal("1"),
        "title",
        "message",
        {"symbol": "SOLUSDT", "ts": "2026-01-01T00:00:40+00:00", "trigger_conditions": ["b"]},
    )
    bundled = aggregate_symbol_alerts([first, second])
    assert len(bundled) == 1
    assert bundled[0].alert_type == "symbol_alert_bundle"
    assert bundled[0].severity == "CRITICAL"
    assert bundled[0].payload["merged_alert_types"] == ["flat_oi_buildup", "buy_absorption"]


def test_same_symbol_alert_aggregation_starts_a_new_bundle_after_30_seconds() -> None:
    items = [
        AlertDecision(
            "a",
            "WARNING",
            "up",
            Decimal("1"),
            "title",
            "message",
            {"symbol": "SOLUSDT", "ts": "2026-01-01T00:00:00+00:00"},
        ),
        AlertDecision(
            "b",
            "WARNING",
            "up",
            Decimal("1"),
            "title",
            "message",
            {"symbol": "SOLUSDT", "ts": "2026-01-01T00:00:31+00:00"},
        ),
    ]
    assert [item.alert_type for item in aggregate_symbol_alerts(items)] == ["a", "b"]


def test_live_preparation_resolves_bundle_alert_types() -> None:
    items = [
        AlertDecision(
            "buy_absorption",
            "WARNING",
            "neutral",
            Decimal("2"),
            "title",
            "message",
            {"symbol": "SOLUSDT", "ts": "2026-01-01T00:00:10+00:00"},
        ),
        AlertDecision(
            "flat_oi_buildup",
            "WARNING",
            "neutral",
            Decimal("1"),
            "title",
            "message",
            {"symbol": "SOLUSDT", "ts": "2026-01-01T00:00:20+00:00"},
        ),
    ]
    prepared, alert_types, symbols = prepare_indicator_decisions_for_mode(
        items,
        Settings(alert_mode="live", symbols=["SOLUSDT"], digest_trigger_count=5),
    )
    assert [item.alert_type for item in prepared] == ["symbol_alert_bundle"]
    assert "symbol_alert_bundle" in alert_types
    assert "market_digest" in alert_types
    assert "MARKET" in symbols


def test_live_preparation_keeps_market_digest_active_key() -> None:
    items = [
        AlertDecision(
            "active_buy_impulse",
            "WARNING",
            "up",
            Decimal(i),
            "title",
            "message",
            {"symbol": f"S{i}", "ts": "2026-01-01T00:00:00+00:00"},
        )
        for i in range(5)
    ]
    prepared, alert_types, symbols = prepare_indicator_decisions_for_mode(
        items,
        Settings(alert_mode="live", symbols=[f"S{i}" for i in range(5)], digest_trigger_count=5),
    )
    assert [item.alert_type for item in prepared] == ["market_digest"]
    assert prepared[0].payload["symbol"] == "MARKET"
    assert "market_digest" in alert_types
    assert "MARKET" in symbols
