# Binance Market Monitor MVP

Binance USD-M futures anomaly monitor for 1m closed klines, open interest, mark price,
liquidation snapshots, market-relative indicators, shadow/live alerts, and Discord delivery.

## Quick Start

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose exec app bn-monitor healthcheck
docker compose exec app bn-monitor config-dump
docker compose exec app bn-monitor test-discord
```

`ALERT_MODE=shadow` stores alerts without sending Discord messages. `ALERT_MODE=live`
stores alerts and sends Discord embeds through `DISCORD_WEBHOOK_URL`.

## Useful Commands

```powershell
pytest
ruff check .
bn-monitor poll-once
bn-monitor config-dump
bn-monitor compute-indicators
bn-monitor generate-alerts
bn-monitor data-quality --lookback-hours 24
bn-monitor data-quality --lookback-hours 24 --max-staleness-minutes 3
bn-monitor alert-summary --lookback-hours 24
bn-monitor alert-summary --symbol SOLUSDT --alert-type active_buy_impulse --severity CRITICAL
bn-monitor healthcheck
```

See [docs/acceptance-checklist.md](docs/acceptance-checklist.md) for the full
deployment and long-run verification checklist. See
[docs/implementation-audit.md](docs/implementation-audit.md) for the current
plan-to-artifact audit.

The MVP intentionally does not collect spot data, full `aggTrade`, local order books,
private user data, on-chain data, or automated trading signals.
