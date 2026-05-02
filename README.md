# Binance Market Monitor

Binance USD-M futures anomaly monitor for 1m closed klines, open interest, mark price,
liquidation snapshots, market-relative indicators, shadow/live alerts, and Discord delivery.

The monitor can run against a fixed symbol list or expand at startup to active Binance
USDT perpetual contracts with a 24h quote-volume liquidity gate. Alerting is designed
around BTC-relative plus market-median-relative confirmation, with live-mode aggregation
and suppression to avoid Discord noise during broad market moves.

## Quick Start

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose exec app bn-monitor healthcheck
docker compose exec app bn-monitor config-dump
```

`ALERT_MODE=shadow` stores alerts without sending Discord messages. `ALERT_MODE=live`
stores alerts and sends Discord embeds through `DISCORD_WEBHOOK_URL`.

Run `docker compose exec app bn-monitor test-discord` after setting a webhook.

## Universe Modes

Use the explicit MVP list:

```env
UNIVERSE_MODE=configured
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,LTCUSDT
```

Or sync active Binance USD-M USDT perpetuals once at startup:

```env
UNIVERSE_MODE=all_usdt_perpetual
MIN_24H_QUOTE_VOLUME_USD=5000000
EXCLUDED_SYMBOLS=
```

In all-universe mode, symbols missing 24h ticker `quoteVolume` are excluded. The
universe is not refreshed while the process is running; restart the app to resync.

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

`alert-projection` replays current rules over stored indicator snapshots, which is the
recommended way to tune thresholds before switching from shadow to live.

`DATA_RETENTION_DAYS=30` controls `bn-monitor retention-run`, which deletes old
market time-series rows while leaving `symbols`, `alert_cooldowns`, and alerts intact.
`OPEN_INTEREST_POLL_INTERVAL_SECONDS=300` keeps the 5m open-interest history
polling cadence within Binance's endpoint limits for a large universe.

## Strategy Notes

- Directional alt alerts require both BTC-relative and market-median-relative returns
  to cross the configured threshold.
- BTC/ETH alerts use market-relative returns directly because BTC-relative for BTC is
  always zero.
- `flat_oi_buildup` requires normalized OI buildup, a minimum 15m OI bps move,
  flat normalized price movement, and p90+ volume.
- Live mode bundles same-symbol multi-signals, emits market digests for broad
  co-movement, and caps per-cycle Discord sends.
- Discord embeds are Chinese-first with English metric keys for code/log lookup.

See [docs/strategy.md](docs/strategy.md) for the full strategy specification,
[docs/acceptance-checklist.md](docs/acceptance-checklist.md) for deployment checks,
and [docs/implementation-audit.md](docs/implementation-audit.md) for the current
plan-to-artifact audit.

## Data Boundaries

The MVP intentionally does not collect spot data, full `aggTrade`, local order books,
private user data, on-chain data, or automated trading signals.
