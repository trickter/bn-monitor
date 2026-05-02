from __future__ import annotations

import asyncio
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
        {"name": "Symbol", "value": alert["symbol"], "inline": True},
        {"name": "Direction", "value": alert["direction"], "inline": True},
        {"name": "Severity", "value": alert["severity"], "inline": True},
        {"name": "Trigger Conditions", "value": ", ".join(payload.get("trigger_conditions", [])) or "n/a"},
        {"name": "Core Metrics", "value": _metrics(payload)},
        {"name": "Market Context", "value": _market_context(payload)},
        {"name": "Payload ID", "value": str(alert_id or "pending"), "inline": True},
    ]
    return {
        "embeds": [
            {
                "title": alert["title"],
                "description": alert["message"],
                "color": _color(alert["severity"]),
                "fields": fields,
                "timestamp": alert["ts"].isoformat() if hasattr(alert["ts"], "isoformat") else None,
            }
        ]
    }


def _metrics(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"volume percentile: {payload.get('volume_percentile', 'n/a')}",
            f"volume robust z: {payload.get('volume_robust_z', 'n/a')}",
            f"taker buy ratio: {payload.get('taker_buy_ratio', 'n/a')}",
            f"OI robust z: {payload.get('oi_robust_z', 'n/a')}",
            f"OI move norm 15m: {payload.get('oi_move_norm_15m', 'n/a')}",
            f"funding rate: {payload.get('funding_rate', 'n/a')}",
            f"funding percentile: {payload.get('funding_percentile', 'n/a')}",
        ]
    )


def _market_context(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"BTC return 1m: {payload.get('btc_return_1m', 'n/a')}",
            f"BTC-relative return: {payload.get('btc_relative_return_1m', 'n/a')}",
        ]
    )


def _color(severity: str) -> int:
    return 0xD73A49 if severity == "CRITICAL" else 0xDBAB09
