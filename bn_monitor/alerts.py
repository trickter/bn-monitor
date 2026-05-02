from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from bn_monitor import repository
from bn_monitor.config import Settings
from bn_monitor.indicators import percentile_rank


class DiscordSender(Protocol):
    async def send_alert(self, alert: dict[str, Any], alert_id: int | None) -> bool: ...


INDICATOR_ALERT_TYPES = [
    "active_buy_impulse",
    "active_sell_impulse",
    "buy_absorption",
    "sell_absorption",
    "flat_oi_buildup",
]
LIVE_AGGREGATE_ALERT_TYPES = ["symbol_alert_bundle", "market_digest"]
ALERT_EXPIRE_AFTER = timedelta(hours=6)


@dataclass
class AlertDecision:
    alert_type: str
    severity: str
    direction: str
    score: Decimal
    title: str
    message: str
    payload: dict[str, Any]


def _as_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def generate_alerts(indicator: dict[str, Any], settings: Settings) -> list[AlertDecision]:
    rel = _as_decimal(indicator.get("btc_relative_return_1m"))
    rel_bps = rel * Decimal("10000")
    vol_pct = _as_decimal(indicator.get("volume_percentile"))
    vol_z = _as_decimal(indicator.get("volume_robust_z"))
    buy_ratio = _as_decimal(indicator.get("taker_buy_ratio"), "0.5")
    body = _as_decimal(indicator.get("candle_body_ratio"), "1")
    range_bps = _as_decimal(indicator.get("candle_range_bps"), "0")
    oi_z = _as_decimal(indicator.get("oi_robust_z"))
    volume_ok = (
        vol_pct >= Decimal(str(settings.volume_percentile_threshold))
        or vol_z >= Decimal(str(settings.volume_robust_z_threshold))
    )
    alerts: list[AlertDecision] = []
    common_payload = {
        "symbol": indicator["symbol"],
        "ts": indicator["ts"].isoformat() if hasattr(indicator["ts"], "isoformat") else str(indicator["ts"]),
        "btc_return_1m": str(_as_decimal(indicator.get("btc_return_1m"))),
        "btc_relative_return_1m": str(rel),
        "volume_percentile": str(vol_pct),
        "volume_robust_z": str(vol_z),
        "taker_buy_ratio": str(buy_ratio),
        "oi_robust_z": str(oi_z),
        "oi_change_15m": str(_as_decimal(indicator.get("oi_change_15m"))),
        "oi_move_norm_15m": str(_as_decimal(indicator.get("oi_move_norm_15m"))),
        "funding_rate": str(_as_decimal(indicator.get("funding_rate"))),
        "funding_percentile": str(_as_decimal(indicator.get("funding_percentile"))),
        "trigger_conditions": [],
    }

    threshold = Decimal(str(settings.price_threshold_bps))
    if rel_bps > threshold and volume_ok and buy_ratio >= Decimal("0.75"):
        severity = "CRITICAL" if oi_z >= Decimal(str(settings.oi_buildup_threshold)) else "WARNING"
        payload = {**common_payload, "trigger_conditions": ["relative price up", "volume anomaly", "active buy"]}
        alerts.append(AlertDecision(
            "active_buy_impulse", severity, "up", abs(rel_bps) + vol_z,
            f"{indicator['symbol']} upward active-buy impulse",
            "Price is outperforming BTC with abnormal volume and strong taker-buy pressure.",
            payload,
        ))

    if rel_bps < -threshold and volume_ok and buy_ratio <= Decimal("0.25"):
        severity = "CRITICAL" if oi_z >= Decimal(str(settings.oi_buildup_threshold)) else "WARNING"
        payload = {**common_payload, "trigger_conditions": ["relative price down", "volume anomaly", "active sell"]}
        alerts.append(AlertDecision(
            "active_sell_impulse", severity, "down", abs(rel_bps) + vol_z,
            f"{indicator['symbol']} downward active-sell impulse",
            "Price is underperforming BTC with abnormal volume and strong taker-sell pressure.",
            payload,
        ))

    small_move = Decimal(str(settings.small_move_threshold_bps))
    range_not_extreme = range_bps <= Decimal("200")
    if abs(rel_bps) < small_move and volume_ok and body <= Decimal("0.35") and range_not_extreme:
        if buy_ratio >= Decimal("0.75"):
            payload = {**common_payload, "trigger_conditions": ["active buy", "small close move", "small body"]}
            alerts.append(AlertDecision(
                "buy_absorption", "WARNING", "neutral", vol_z,
                f"{indicator['symbol']} buy-side absorption",
                "High taker-buy volume did not move price materially, suggesting buy-side absorption.",
                payload,
            ))
        if buy_ratio <= Decimal("0.25"):
            payload = {**common_payload, "trigger_conditions": ["active sell", "small close move", "small body"]}
            alerts.append(AlertDecision(
                "sell_absorption", "WARNING", "neutral", vol_z,
                f"{indicator['symbol']} sell-side absorption",
                "High taker-sell volume did not move price materially, suggesting sell-side absorption.",
                payload,
            ))

    price_norm = indicator.get("price_move_norm_15m")
    oi_norm = indicator.get("oi_move_norm_15m")
    if (
        price_norm is not None
        and oi_norm is not None
        and _as_decimal(oi_norm) >= Decimal(str(settings.oi_buildup_threshold))
        and _as_decimal(price_norm) <= Decimal(str(settings.price_flat_norm_threshold))
        and vol_pct >= Decimal("0.70")
    ):
        payload = {
            **common_payload,
            "trigger_conditions": ["OI buildup", "volatility-normalized flat price", "volume support"],
            "price_move_norm_15m": str(price_norm),
            "oi_move_norm_15m": str(oi_norm),
        }
        alerts.append(AlertDecision(
            "flat_oi_buildup", "WARNING", "neutral", oi_z,
            f"{indicator['symbol']} flat-price OI buildup",
            "Price is relatively flat while open interest builds, indicating leverage divergence.",
            payload,
        ))
    return alerts


def generate_liquidation_snapshot_confirmation(
    *,
    symbol: str,
    ts: datetime,
    side: str,
    quote_value: Decimal,
    quote_sample: list[Decimal],
    indicator: dict[str, Any],
) -> AlertDecision | None:
    percentile = percentile_rank(quote_value, quote_sample)
    if percentile is None or percentile < Decimal("0.99"):
        return None

    direction = "up" if side.upper() == "BUY" else "down"
    rel = _as_decimal(indicator.get("btc_relative_return_1m"))
    oi_change = _as_decimal(indicator.get("oi_change_15m"))
    price_same_direction = rel > 0 if direction == "up" else rel < 0
    if not price_same_direction or oi_change >= 0:
        return None

    payload = {
        "symbol": symbol,
        "ts": ts.isoformat(),
        "side": side.upper(),
        "direction": direction,
        "liquidation_snapshot_quote_value": str(quote_value),
        "liquidation_snapshot_quote_1m_percentile": str(percentile),
        "btc_relative_return_1m": str(rel),
        "oi_change_15m": str(oi_change),
        "trigger_conditions": [
            "liquidation snapshot amplification",
            "same-direction price move",
            "open interest contraction",
        ],
    }
    return AlertDecision(
        "liquidation_snapshot_confirmation",
        "WARNING",
        direction,
        percentile * Decimal("100"),
        f"{symbol} liquidation snapshot amplification",
        "A sampled liquidation snapshot is amplified with same-direction price movement and falling OI.",
        payload,
    )


class AlertService:
    def __init__(self, settings: Settings, discord: DiscordSender | None = None) -> None:
        self.settings = settings
        self.discord = discord
        self._memory_cooldowns: dict[str, datetime] = {}

    def should_send(self, alert: AlertDecision, now: datetime) -> bool:
        key = f"{alert.payload['symbol']}:{alert.alert_type}"
        last = self._memory_cooldowns.get(key)
        window = cooldown_window(alert.severity)
        if last and now - last < window:
            return False
        self._memory_cooldowns[key] = now
        return True

    async def should_send_with_persistent_cooldown(
        self, session: AsyncSession, alert: AlertDecision, now: datetime
    ) -> bool:
        key = f"{alert.payload['symbol']}:{alert.alert_type}"
        window = cooldown_window(alert.severity)
        last = self._memory_cooldowns.get(key)
        if last and now - last < window:
            return False
        cooldown = await repository.get_cooldown(session, key)
        if cooldown and now - cooldown["last_sent_at"] < window:
            self._memory_cooldowns[key] = cooldown["last_sent_at"]
            return False
        self._memory_cooldowns[key] = now
        return True

    async def persist_and_deliver(self, session: AsyncSession, decision: AlertDecision) -> int | None:
        now = datetime.now(UTC)
        source_ts = _parse_source_ts(decision.payload.get("ts"), now)
        symbol = str(decision.payload.get("symbol", "MARKET"))
        parent = await repository.find_recent_active_alert(
            session,
            symbol,
            decision.alert_type,
            self.settings.alert_mode,
            source_ts - timedelta(minutes=15),
        )
        alert = {
            "ts": source_ts,
            "symbol": symbol,
            "alert_type": decision.alert_type,
            "severity": decision.severity,
            "direction": decision.direction,
            "state": "escalated" if parent else "open",
            "score": decision.score,
            "title": decision.title,
            "message": decision.message,
            "payload": decision.payload,
            "parent_alert_id": parent["id"] if parent else None,
        }
        if self.settings.alert_mode == "shadow":
            alert_key = await repository.insert_alert(session, alert, "shadow", "shadow")
            return alert_key[1] if alert_key else None
        if not await self.should_send_with_persistent_cooldown(session, decision, now):
            alert_key = await repository.insert_alert(session, alert, "live", "suppressed")
            return alert_key[1] if alert_key else None
        alert_key = await repository.insert_alert(session, alert, "live", "pending")
        if alert_key is None:
            return None
        alert_ts, alert_id = alert_key
        sent = False
        if self.discord:
            sent = await self.discord.send_alert(alert, alert_id)
        delivery_status = getattr(self.discord, "last_delivery_status", None)
        await repository.update_alert_delivery(
            session,
            alert_ts,
            alert_id,
            delivery_status or ("sent" if sent else "failed"),
            datetime.now(UTC) if sent else None,
        )
        await self.record_cooldown(session, alert, decision.score, now)
        return alert_id

    async def record_cooldown(
        self, session: AsyncSession, alert: dict[str, Any], score: Decimal, now: datetime
    ) -> None:
        key = f"{alert['symbol']}:{alert['alert_type']}"
        current = await repository.get_cooldown(session, key)
        count_1h = 1
        if current and now - current["last_sent_at"] <= timedelta(hours=1):
            count_1h = int(current["count_1h"]) + 1
        await repository.update_cooldown(session, key, now, score, count_1h)


def cooldown_window(severity: str) -> timedelta:
    return timedelta(minutes=5 if severity == "CRITICAL" else 10)


def build_market_digest(decisions: list[AlertDecision], trigger_count: int) -> AlertDecision | None:
    grouped: dict[datetime, list[AlertDecision]] = {}
    for decision in decisions:
        ts = _parse_source_ts(decision.payload.get("ts"), datetime.now(UTC))
        minute = ts.replace(second=0, microsecond=0)
        grouped.setdefault(minute, []).append(decision)

    eligible = [items for items in grouped.values() if len(items) >= trigger_count]
    if not eligible:
        return None
    decisions = max(eligible, key=lambda items: _parse_source_ts(items[0].payload.get("ts"), datetime.now(UTC)))
    ordered = sorted(decisions, key=lambda item: item.score, reverse=True)
    top = ordered[:10]
    symbols = [item.payload["symbol"] for item in top]
    payload = {
        "symbol": "MARKET",
        "ts": top[0].payload.get("ts"),
        "symbols": symbols,
        "alert_ids": [],
        "trigger_conditions": ["market co-movement digest"],
        "top_movers": [
            {"symbol": item.payload["symbol"], "alert_type": item.alert_type, "score": str(item.score)}
            for item in top
        ],
    }
    return AlertDecision(
        "market_digest",
        "CRITICAL" if any(item.severity == "CRITICAL" for item in top) else "WARNING",
        "neutral",
        sum((item.score for item in top), Decimal("0")),
        "Market co-movement digest",
        f"Multiple symbols triggered in the same minute. Top movers: {', '.join(symbols)}.",
        payload,
    )


def prioritize_alerts(decisions: list[AlertDecision]) -> list[AlertDecision]:
    return sorted(
        decisions,
        key=lambda decision: (0 if decision.severity == "CRITICAL" else 1, -decision.score),
    )


def aggregate_symbol_alerts(decisions: list[AlertDecision]) -> list[AlertDecision]:
    by_symbol: dict[str, list[AlertDecision]] = {}
    for decision in decisions:
        symbol = str(decision.payload.get("symbol", "UNKNOWN"))
        by_symbol.setdefault(symbol, []).append(decision)

    aggregated: list[AlertDecision] = []
    for symbol_items in by_symbol.values():
        ordered_items = sorted(
            symbol_items,
            key=lambda item: _parse_source_ts(item.payload.get("ts"), datetime.now(UTC)),
        )
        current: list[AlertDecision] = []
        current_start: datetime | None = None
        for item in ordered_items:
            item_ts = _parse_source_ts(item.payload.get("ts"), datetime.now(UTC))
            if current_start is None or item_ts - current_start <= timedelta(seconds=30):
                current.append(item)
                current_start = current_start or item_ts
                continue
            aggregated.append(_aggregate_symbol_group(current))
            current = [item]
            current_start = item_ts
        if current:
            aggregated.append(_aggregate_symbol_group(current))
    return aggregated


def _aggregate_symbol_group(items: list[AlertDecision]) -> AlertDecision:
    if not items:
        raise ValueError("cannot aggregate an empty alert group")
    if len(items) == 1:
        return items[0]
    ordered = prioritize_alerts(items)
    primary = ordered[0]
    payload = {
        **primary.payload,
        "symbol": primary.payload.get("symbol"),
        "ts": primary.payload.get("ts"),
        "trigger_conditions": sorted(
            {
                condition
                for item in ordered
                for condition in item.payload.get("trigger_conditions", [])
            }
        ),
        "merged_alert_types": [item.alert_type for item in ordered],
        "merged_alert_count": len(ordered),
        "component_scores": {
            item.alert_type: str(item.score)
            for item in ordered
        },
    }
    return AlertDecision(
        "symbol_alert_bundle",
        primary.severity,
        primary.direction,
        sum((item.score for item in ordered), Decimal("0")),
        f"{primary.payload.get('symbol')} multi-signal alert bundle",
        "Multiple alert types triggered for the same symbol within 30 seconds.",
        payload,
    )


def _parse_source_ts(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


def prepare_indicator_decisions_for_mode(
    decisions: list[AlertDecision],
    settings: Settings,
) -> tuple[list[AlertDecision], list[str], list[str]]:
    if settings.alert_mode != "live":
        return decisions, INDICATOR_ALERT_TYPES, settings.symbols

    digest = build_market_digest(decisions, settings.digest_trigger_count)
    prepared = [digest] if digest is not None else aggregate_symbol_alerts(decisions)
    return (
        prepared,
        [*INDICATOR_ALERT_TYPES, *LIVE_AGGREGATE_ALERT_TYPES],
        [*settings.symbols, "MARKET"],
    )


async def fetch_latest_indicator_snapshots(
    session: AsyncSession, symbols: list[str]
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (symbol) *
                FROM (
                    SELECT
                        i.*,
                        mf.btc_return_1m
                    FROM indicator_snapshot_1m i
                    LEFT JOIN LATERAL (
                        SELECT btc_return_1m
                        FROM market_factor_1m
                        WHERE ts <= i.ts
                        ORDER BY ts DESC
                        LIMIT 1
                    ) mf ON TRUE
                    WHERE i.symbol IN :symbols
                ) latest
                ORDER BY symbol, ts DESC
                """
            ).bindparams(bindparam("symbols", expanding=True)),
            {"symbols": symbols},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def process_latest_indicator_alerts(
    session: AsyncSession, settings: Settings, service: AlertService
) -> int:
    await repository.expire_stale_alerts(session, datetime.now(UTC) - ALERT_EXPIRE_AFTER)
    snapshots = await fetch_latest_indicator_snapshots(session, settings.symbols)
    decisions: list[AlertDecision] = []
    for snapshot in snapshots:
        decisions.extend(generate_alerts(snapshot, settings))
    decisions, alert_types_to_resolve, symbols_to_resolve = prepare_indicator_decisions_for_mode(
        decisions, settings
    )
    active_keys = {(decision.payload["symbol"], decision.alert_type) for decision in decisions}
    await repository.resolve_inactive_alerts(
        session,
        symbols_to_resolve,
        alert_types_to_resolve,
        active_keys,
        settings.alert_mode,
    )

    stored = 0
    for decision in prioritize_alerts(decisions):
        await service.persist_and_deliver(session, decision)
        stored += 1
    return stored


async def process_latest_liquidation_confirmations(
    session: AsyncSession, settings: Settings, service: AlertService
) -> int:
    rows = (
        await session.execute(
            text(
                """
                WITH latest_minute AS (
                    SELECT date_trunc('minute', max(ts)) AS minute_ts
                    FROM liquidation_snapshots
                    WHERE symbol IN :symbols
                )
                SELECT
                    l.symbol,
                    l.side,
                    date_trunc('minute', l.ts) AS minute_ts,
                    sum(l.quote_value) AS quote_value
                FROM liquidation_snapshots l
                JOIN latest_minute m ON date_trunc('minute', l.ts) = m.minute_ts
                WHERE l.symbol IN :symbols
                GROUP BY l.symbol, l.side, date_trunc('minute', l.ts)
                """
            ).bindparams(bindparam("symbols", expanding=True)),
            {"symbols": settings.symbols},
        )
    ).mappings().all()
    stored = 0
    for row in rows:
        symbol = row["symbol"]
        side = row["side"]
        minute_ts = row["minute_ts"]
        sample = await _liquidation_quote_sample(session, symbol, side, minute_ts)
        indicator = await _indicator_at_or_before(session, symbol, minute_ts)
        if not indicator:
            continue
        decision = generate_liquidation_snapshot_confirmation(
            symbol=symbol,
            ts=minute_ts,
            side=side,
            quote_value=row["quote_value"],
            quote_sample=sample,
            indicator=indicator,
        )
        if decision is None:
            continue
        await service.persist_and_deliver(session, decision)
        stored += 1
    return stored


async def _liquidation_quote_sample(
    session: AsyncSession, symbol: str, side: str, ts: datetime
) -> list[Decimal]:
    rows = (
        await session.execute(
            text(
                """
                SELECT sum(quote_value) AS quote_value
                FROM liquidation_snapshots
                WHERE symbol = :symbol
                  AND side = :side
                  AND ts < :ts
                  AND ts >= :ts - interval '7 days'
                GROUP BY date_trunc('minute', ts)
                ORDER BY date_trunc('minute', ts) DESC
                LIMIT 10080
                """
            ),
            {"symbol": symbol, "side": side, "ts": ts},
        )
    ).all()
    return [row[0] for row in rows if row[0] is not None]


async def _indicator_at_or_before(
    session: AsyncSession, symbol: str, ts: datetime
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT *
                FROM indicator_snapshot_1m
                WHERE symbol = :symbol AND ts <= :ts
                ORDER BY ts DESC
                LIMIT 1
                """
            ),
            {"symbol": symbol, "ts": ts},
        )
    ).mappings().first()
    return dict(row) if row else None
