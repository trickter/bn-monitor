from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = float(capacity)
        self._updated_at: float | None = None

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        if self._updated_at is None:
            self._updated_at = loop.time()
        while True:
            now = loop.time()
            elapsed = now - self._updated_at
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self._updated_at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep(max((1 - self.tokens) / self.refill_per_second, 0.05))


class DiscordWebhook:
    def __init__(
        self,
        webhook_url: str,
        capacity: int = 5,
        refill_per_second: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.bucket = TokenBucket(capacity, refill_per_second)
        self.client = client or httpx.AsyncClient(timeout=10)
        self.last_delivery_status = "pending"

    async def close(self) -> None:
        await self.client.aclose()

    async def send_alert(self, alert: dict[str, Any], alert_id: int | None) -> bool:
        await self.bucket.acquire()
        payload = discord_payload(alert, alert_id)
        saw_rate_limit = False
        for attempt in range(3):
            response = await self.client.post(self.webhook_url, json=payload)
            if response.status_code in {200, 204}:
                await _respect_success_rate_limit(response)
                self.last_delivery_status = "sent"
                return True
            if response.status_code == 429:
                saw_rate_limit = True
                retry_after = _retry_after(response)
                log.warning("discord_rate_limited", retry_after=retry_after, attempt=attempt)
                await asyncio.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                await asyncio.sleep(2**attempt)
                continue
            log.warning("discord_send_failed", status_code=response.status_code, body=response.text[:500])
            self.last_delivery_status = "failed"
            return False
        self.last_delivery_status = "rate_limited" if saw_rate_limit else "failed"
        return False


def _retry_after(response: httpx.Response) -> float:
    try:
        return float(response.json().get("retry_after", 1))
    except Exception:
        header = response.headers.get("Retry-After")
        return float(header) if header else 1.0


async def _respect_success_rate_limit(response: httpx.Response) -> None:
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset_after = response.headers.get("X-RateLimit-Reset-After")
    if remaining == "0" and reset_after:
        await asyncio.sleep(float(reset_after))


def discord_payload(alert: dict[str, Any], alert_id: int | None) -> dict[str, Any]:
    payload = alert.get("payload", {})
    fields = [
        {"name": "交易对 (symbol)", "value": alert["symbol"], "inline": True},
        {"name": "方向 (direction)", "value": alert["direction"], "inline": True},
        {"name": "级别 (severity)", "value": alert["severity"], "inline": True},
        {"name": "触发条件 (conditions)", "value": ", ".join(payload.get("trigger_conditions", [])) or "n/a"},
        {"name": "核心指标 (metrics)", "value": _metrics(payload)},
        {"name": "市场背景 (context)", "value": _market_context(payload)},
        {"name": "Payload ID", "value": _payload_id_text(alert_id), "inline": False},
    ]
    return {
        "embeds": [
            {
                "title": _title(alert),
                "description": alert["message"],
                "color": _color(alert["severity"], alert.get("alert_type")),
                "fields": fields,
                "timestamp": alert["ts"].isoformat() if hasattr(alert["ts"], "isoformat") else None,
            }
        ]
    }


def _metrics(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"成交量分位 (volume_pct): {_format_percentile(payload.get('volume_percentile'))}",
            f"成交量 robust z (volume_z): {_format_decimal(payload.get('volume_robust_z'))}",
            f"主动买入比 (taker_buy): {format_pct(payload.get('taker_buy_ratio'))}",
            f"OI robust z (oi_z): {_format_decimal(payload.get('oi_robust_z'))}",
            f"OI 15m 标准化 (oi_norm): {_format_decimal(payload.get('oi_move_norm_15m'))}",
            f"OI 15m 变化 (oi_bps): {format_bps(payload.get('oi_change_15m'))}",
            f"资金费率 (funding): {format_funding_rate(payload.get('funding_rate'))}",
            f"资金费率分位 (funding_pct): {_format_percentile(payload.get('funding_percentile'))}",
        ]
    )


def _market_context(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"BTC 1m 收益 (btc_ret): {format_pct(payload.get('btc_return_1m'))}",
            f"BTC 相对收益 (btc_rel): {format_pct(payload.get('btc_relative_return_1m'))}",
            f"市场中位收益 (market_median): {format_pct(payload.get('market_median_return_1m'))}",
            f"市场相对收益 (market_rel): {format_pct(payload.get('market_relative_return_1m'))}",
        ]
    )


def _color(severity: str, alert_type: str | None = None) -> int:
    type_colors = {
        "flat_oi_buildup": 0x8B949E,
        "active_buy_impulse": 0x2EA043,
        "buy_absorption": 0x2EA043,
        "active_sell_impulse": 0xD73A49,
        "sell_absorption": 0xD73A49,
        "liquidation_snapshot_confirmation": 0x8957E5,
        "market_digest": 0xDBAB09,
        "symbol_alert_bundle": 0x58A6FF,
    }
    if alert_type in type_colors:
        return type_colors[alert_type]
    return 0xD73A49 if severity == "CRITICAL" else 0xDBAB09


def _title(alert: dict[str, Any]) -> str:
    title_by_type = {
        "flat_oi_buildup": "横盘增仓",
        "active_buy_impulse": "主动买入冲击",
        "active_sell_impulse": "主动卖出冲击",
        "buy_absorption": "买盘吸收",
        "sell_absorption": "卖盘吸收",
        "liquidation_snapshot_confirmation": "强平快照确认",
        "market_digest": "市场共振摘要",
        "symbol_alert_bundle": "多信号合并",
    }
    alert_type = alert.get("alert_type")
    prefix = title_by_type.get(alert_type)
    if prefix:
        return f"{alert['symbol']} {prefix}"
    return alert["title"]


def _payload_id_text(alert_id: int | None) -> str:
    if alert_id is None:
        return "pending"
    return f"{alert_id} (`bn-monitor alert-show {alert_id}`)"


def _to_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _format_decimal(value: Any, places: int = 2) -> str:
    decimal = _to_decimal(value)
    if decimal is None:
        return "n/a"
    return f"{decimal:.{places}f}"


def format_pct(value: Any, places: int = 2) -> str:
    decimal = _to_decimal(value)
    if decimal is None:
        return "n/a"
    sign = "+" if decimal > 0 else ""
    return f"{sign}{decimal * Decimal('100'):.{places}f}%"


def format_bps(value: Any, places: int = 1) -> str:
    decimal = _to_decimal(value)
    if decimal is None:
        return "n/a"
    sign = "+" if decimal > 0 else ""
    return f"{sign}{decimal * Decimal('10000'):.{places}f} bps"


def format_funding_rate(value: Any) -> str:
    return format_pct(value, places=4)


def _format_percentile(value: Any, places: int = 1) -> str:
    decimal = _to_decimal(value)
    if decimal is None:
        return "n/a"
    return f"p{decimal * Decimal('100'):.{places}f}"
