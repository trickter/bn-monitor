from __future__ import annotations

import httpx
import pytest

from bn_monitor.discord import DiscordWebhook


@pytest.mark.asyncio
async def test_discord_respects_success_rate_limit_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            204,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.5"},
            request=request,
        )

    monkeypatch.setattr("bn_monitor.discord.asyncio.sleep", fake_sleep)
    webhook = DiscordWebhook(
        "https://discord.test/webhook",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        sent = await webhook.send_alert(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "direction": "up",
                "severity": "WARNING",
                "title": "test",
                "message": "test",
                "payload": {},
            },
            1,
        )
    finally:
        await webhook.close()

    assert sent
    assert webhook.last_delivery_status == "sent"
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_discord_marks_rate_limited_after_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"retry_after": 0.01}, request=request)

    monkeypatch.setattr("bn_monitor.discord.asyncio.sleep", fake_sleep)
    webhook = DiscordWebhook(
        "https://discord.test/webhook",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        sent = await webhook.send_alert(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "direction": "up",
                "severity": "WARNING",
                "title": "test",
                "message": "test",
                "payload": {},
            },
            1,
        )
    finally:
        await webhook.close()

    assert not sent
    assert webhook.last_delivery_status == "rate_limited"
