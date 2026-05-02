# MVP Acceptance Checklist

Use this checklist after deploying the stack in an environment with Docker,
TimescaleDB, Binance network access, and an optional Discord webhook.

## Phase 0 - Skeleton

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
docker compose exec app bn-monitor healthcheck
docker compose exec app bn-monitor config-dump
```

Expected evidence:

- `timescaledb` and `app` containers are healthy.
- `bn-monitor healthcheck` prints `ok`.
- `bn-monitor config-dump` shows the expected `symbols`, `alert_mode`, thresholds,
  and redacted secrets.
- The app container uses `timescaledb` as its database host, not `localhost`.
- Alembic has applied `0001_initial`.
- Timescale hypertables exist for kline, open interest, mark price, liquidation snapshots,
  market factors, indicator snapshots, and alerts.

## Discord

```powershell
$env:DISCORD_WEBHOOK_URL = "<webhook>"
bn-monitor test-discord
```

Expected evidence:

- A test embed appears in Discord.
- On HTTP 429, the client retries according to `retry_after`.

## Phase 1 - Collection

```powershell
bn-monitor poll-once
bn-monitor data-quality --lookback-hours 24 --max-staleness-minutes 3
```

Expected evidence:

- `exchangeInfo`, `openInterest`, and `premiumIndex` data are persisted.
- Recent closed `kline_1m` rows are backfilled from REST and upserted to heal
  short restart or WebSocket gaps.
- Funding history from `/fapi/v1/fundingRate` is seeded into `futures_mark_price`
  for percentile warmup.
- Only closed `kline_1m` rows are persisted.
- The 24h gap ratio for every configured symbol is `<= KLINE_GAP_MAX_RATIO`.
- The latest closed `kline_1m` row for every configured symbol is no older than
  `KLINE_MAX_STALENESS_MINUTES`.
- `open_interest` and `mark_price` freshness reports are `ok=true` for every
  configured symbol.

## Phase 2 - Indicators

```powershell
bn-monitor compute-indicators
```

Expected evidence:

- `indicator_snapshot_1m` rows are created for the latest common closed minute.
- `INDICATOR_POLL_INTERVAL_SECONDS` is `<= 10`, so compute latency is bounded by the
  configured loop interval plus query time.
- Warmup symbols without baseline do not generate alerts.
- Flat OI buildup uses `price_move_norm_15m` and `oi_move_norm_15m`, not a fixed
  percent move floor.
- Funding percentile and OI fields are populated when supporting data exists.

## Phase 3 - Shadow Signals

```powershell
$env:ALERT_MODE = "shadow"
bn-monitor generate-alerts
bn-monitor alert-summary --lookback-hours 24
bn-monitor alert-summary --symbol SOLUSDT --alert-type active_buy_impulse --severity CRITICAL
```

Expected evidence:

- Alerts are stored with `mode='shadow'` and `delivery_status='shadow'`.
- No Discord delivery is attempted.
- Alerts can be queried by `symbol`, `alert_type`, and `severity`.
- Alert states progress through `open`, `escalated`, `resolved`, and `expired`.
- `bn-monitor alert-summary` shows the expected state and delivery-status distribution.

## Phase 4 - Live Alerts

```powershell
$env:ALERT_MODE = "live"
$env:DISCORD_WEBHOOK_URL = "<webhook>"
bn-monitor generate-alerts
bn-monitor alert-summary --lookback-hours 24
```

Expected evidence:

- Discord embeds include symbol, direction, severity, trigger conditions, core metrics,
  BTC market context, explanation, and payload id.
- `delivery_status` records `sent`, `failed`, `rate_limited`, or `suppressed`.
- Multiple alert types for the same symbol within 30 seconds are bundled in live mode.
- Same-minute market co-movement generates one digest instead of per-symbol spam.
- CRITICAL alerts use a 1-minute cooldown; WARNING alerts use a 10-minute cooldown.

## Phase 5 - Futures Context

Expected evidence:

- `liquidation_snapshots` contains sampled force-order snapshots.
- Confirmation signals use `liquidation_snapshot_*` naming and do not claim real total liquidations.
- `open_interest_value` is derived from open interest and mark price.

## Long-Run Stability

Run the service for 24 hours first, then for 7 days:

```powershell
docker compose up -d
docker compose logs --tail=200 app
bn-monitor data-quality --lookback-hours 24
```

Expected evidence:

- The service continues running after transient REST, WebSocket, or database errors.
- WebSocket reconnects automatically.
- `data-quality` continues to show fresh klines and a gap ratio within threshold.
- Logs rotate via Docker `json-file` limits and do not grow without bound.
- Restarting the app resumes collection and alerting.
- Restarting the app backfills recent closed klines according to `KLINE_BACKFILL_LIMIT`.
