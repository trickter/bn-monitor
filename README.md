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
bn-monitor retention-run
bn-monitor alert-projection --hours 48 --profile balanced
bn-monitor alert-show 123
bn-monitor data-quality --lookback-hours 24
bn-monitor data-quality --lookback-hours 24 --max-staleness-minutes 3
bn-monitor alert-summary --lookback-hours 24
bn-monitor alert-summary --symbol SOLUSDT --alert-type active_buy_impulse --severity CRITICAL
bn-monitor healthcheck
```

Set `UNIVERSE_MODE=all_usdt_perpetual` to sync active Binance USD-M `USDT`
perpetual contracts once at startup. In all mode, symbols must also meet
`MIN_24H_QUOTE_VOLUME_USD` based on Binance 24h ticker `quoteVolume` and symbols
missing ticker data are excluded. Leave `UNIVERSE_MODE=configured` to keep the
explicit `SYMBOLS` list without applying the liquidity gate. Use `EXCLUDED_SYMBOLS`
as a manual blacklist for newly listed or noisy contracts.

`DATA_RETENTION_DAYS=30` controls `bn-monitor retention-run`, which deletes old
market time-series rows while leaving `symbols`, `alert_cooldowns`, and alerts intact.
`OPEN_INTEREST_POLL_INTERVAL_SECONDS=300` keeps the 5m open-interest history
polling cadence within Binance's endpoint limits for a large universe.

See [docs/acceptance-checklist.md](docs/acceptance-checklist.md) for the full
deployment and long-run verification checklist. See
[docs/implementation-audit.md](docs/implementation-audit.md) for the current
plan-to-artifact audit.

The MVP intentionally does not collect spot data, full `aggTrade`, local order books,
private user data, on-chain data, or automated trading signals.
