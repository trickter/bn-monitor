# Implementation Audit

Objective: implement `plans/binance-market-monitor-mvp.md` and satisfy its
acceptance criteria.

This audit maps plan requirements to concrete artifacts and verification
commands. Items marked "external verification" require Docker, Binance network
access, a running TimescaleDB, a Discord webhook, or a long-running deployment.

## Requirement Map

| Requirement | Evidence | Status |
| --- | --- | --- |
| Python 3.12+, asyncio, websockets, httpx, SQLAlchemy 2.x, Alembic, pydantic-settings, structlog | `pyproject.toml`, `bn_monitor/` package | Implemented |
| Docker Compose with TimescaleDB and app | `Dockerfile`, `docker-compose.yml` | Implemented; external verification |
| Config system with `ALERT_MODE=shadow/live` and redacted runtime config dump | `bn_monitor/config.py`, `.env.example`, `bn-monitor config-dump` | Implemented and unit-tested |
| JSON logging and bounded Docker logs | `bn_monitor/logging.py`, `docker-compose.yml` logging options | Implemented; external verification |
| Alembic migration and Timescale hypertables | `alembic/versions/0001_initial.py`; `alembic upgrade head --sql` | Verified locally by SQL generation |
| Required tables and replay indexes | `bn_monitor/models.py`, migration indexes | Implemented |
| 10 default USD-M symbols | `bn_monitor/config.py` | Implemented |
| WebSocket `kline_1m`, mark price, force-order snapshots | `bn_monitor/binance.py`, `bn_monitor/app.py` | Implemented; external live data verification |
| Persist only closed klines | `closed_kline_from_ws`; `tests/test_binance.py` | Verified by unit test |
| REST `exchangeInfo`, `openInterest`, `premiumIndex`, funding history with per-symbol seed isolation | `BinanceRestClient`, `seed_funding_history_once`, `poll-once` | Implemented; external live data verification |
| Binance REST 429 retry and transient 5xx retry | `BinanceRestClient._get`; `tests/test_binance.py` | Verified by unit test |
| WebSocket reconnect, per-message DB failure tolerance, REST closed-kline backfill, per-symbol REST failure isolation, and per-symbol DB transaction isolation | `BinanceStream.messages`, `MonitorApp.run_ws`, `BinanceRestClient.klines_1m`, `poll_rest_forever` | Implemented; external long-run verification |
| Idempotent upserts | `bn_monitor/repository.py` | Implemented |
| Kline gap plus open-interest and mark-price freshness report | `bn_monitor/quality.py`, `bn-monitor data-quality` outputs `klines`, `open_interest`, and `mark_price` sections | Implemented; external data verification |
| Indicators: returns, BTC-relative return, volume baseline, taker ratios, candle structure, OI, funding percentile | `bn_monitor/indicators.py`; `tests/test_indicators.py` | Verified by unit tests |
| Alert payloads and Discord metrics explain price, volume, active direction, OI, and funding | `generate_alerts`, `discord_payload`; `tests/test_alerts.py` | Verified by unit tests |
| Indicator latency below 10 seconds | `INDICATOR_POLL_INTERVAL_SECONDS=5` | Implemented; external runtime measurement |
| Warmup symbols do not live-alert without baseline | `generate_alerts`; `tests/test_alerts.py` | Verified by unit test |
| Shadow alerts store without Discord | `AlertService.persist_and_deliver`; `tests/test_alerts.py` | Implemented and unit-tested |
| Query alerts by symbol, alert type, severity | `bn-monitor alert-summary --symbol --alert-type --severity` | Implemented and CLI-verified |
| Active buy/sell, absorption, flat OI buildup | `generate_alerts`; `tests/test_alerts.py` | Verified by unit tests |
| Flat OI uses normalized price/OI moves | `price_move_norm_15m`, `oi_move_norm_15m` | Verified by unit tests |
| Liquidation snapshot confirmation wording and naming | `generate_liquidation_snapshot_confirmation`; tests | Verified by unit tests |
| Discord embeds include symbol, direction, severity, triggers, metrics, BTC context, payload id | `bn_monitor/discord.py`; tests | Verified by unit tests |
| Discord token bucket, success headers, 429 retry | `DiscordWebhook`; `tests/test_discord.py` | Verified by unit tests; external webhook test required |
| Same-symbol multiple alert types bundle within 30 seconds | `aggregate_symbol_alerts`; tests | Verified by unit test |
| Market co-movement digest | `build_market_digest`; tests | Verified by unit tests |
| CRITICAL priority and cooldown behavior | `prioritize_alerts`, `cooldown_window`; tests | Verified by unit tests |
| Alert states open/escalated/resolved/expired, including live bundles and market digests | `repository.py`, `AlertService`, `prepare_indicator_decisions_for_mode`; tests | Implemented and unit-tested |
| 24h and 7d stability, restart resume, real gap ratio < 0.1% | `docs/acceptance-checklist.md` | External verification required |

## Local Verification

Last local checks:

```powershell
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev pytest
uv run --python 3.12 --extra dev alembic upgrade head --sql
uv run --python 3.12 --extra dev bn-monitor --help
uv run --python 3.12 --extra dev bn-monitor alert-summary --help
uv run --python 3.12 --extra dev bn-monitor config-dump
```

Observed result: ruff passed, pytest passed with 51 tests, Alembic SQL generation
completed, CLI help loaded, and `config-dump` printed redacted runtime settings.

## Remaining External Gates

The objective is not fully complete until these are run in a deployment
environment:

- `docker compose up -d --build`
- `docker compose exec app bn-monitor healthcheck`
- `bn-monitor test-discord` with a real `DISCORD_WEBHOOK_URL`
- 24-hour data-quality check with `bn-monitor data-quality --lookback-hours 24`
- 7-day stability run with restart and log-rotation inspection
