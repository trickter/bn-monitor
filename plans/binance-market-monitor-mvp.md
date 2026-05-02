# Binance 二级市场异动监控系统 MVP 总体方案

## 1. 方案定位

本系统第一版定位为：

```text
Binance U 本位合约异动监控
- 不做链上
- 不做自动交易
- 不接用户私有账户
- 不做多交易所套利
- 不追求全市场高频原始数据落库
```

MVP 的核心目标不是“指标尽可能多”，而是先跑通一条稳定、可复盘、不会刷屏的链路：

```text
Binance Futures Public Data
  -> 1m K线 / OI / Mark Price / Funding / ForceOrder Snapshot
  -> 市场相对指标
  -> 价格 × 量能 × 主动方向 × OI 结构信号
  -> Shadow / Live 告警
  -> Discord Webhook
  -> TimescaleDB 复盘
```

第一版优先监控：

```text
BTCUSDT、ETHUSDT、SOLUSDT、BNBUSDT、XRPUSDT、DOGEUSDT、
ADAUSDT、LINKUSDT、AVAXUSDT、LTCUSDT
```

后续稳定后再扩展到全 USDT 永续。

## 2. 核心技术决策

### 数据库

采用：

```text
TimescaleDB = PostgreSQL + 时序扩展
```

原因：

```text
保留 PostgreSQL SQL / 生态 / Alembic 迁移体验
支持 hypertable、自动 chunk、压缩、连续聚合
比 vanilla PostgreSQL 更适合 K 线、指标、告警、baseline 这类时序数据
```

第一版不建议直接写入全量 `aggTrade` 和全量 `depth@100ms`。这些数据量会很快把系统复杂度推高。

### 后端技术栈

```text
Python 3.12+
asyncio
websockets 或 aiohttp
httpx
SQLAlchemy 2.x
Alembic
pydantic-settings
structlog / loguru
Docker Compose
TimescaleDB
Discord Webhook
```

### 告警模式

必须支持：

```text
ALERT_MODE=shadow | live
```

默认先用 `shadow` 跑 3-7 天：

```text
shadow：只落库，不发 Discord
live：落库并发送 Discord
```

这是避免阈值未调好时把 Discord 频道炸掉的关键设计。

## 3. 数据接入设计

### MVP 接入数据

第一版只接 U 本位合约：

```text
WebSocket:
- <symbol>@kline_1m
- !markPrice@arr@1s 或 !markPrice@arr
- !forceOrder@arr

REST Poller:
- exchangeInfo
- openInterest
- fundingRate 或 funding 历史
```

注意事项：

```text
kline 必须只在 x=true 时入库，避免未收盘 K 线污染指标
所有时间以 Binance event time / kline close time 为准，不用本地时间判断行情
WebSocket 要处理 24h 断连、ping/pong、重连、订阅分片
forceOrder 只能视为“强平快照采样”，不能当真实全市场强平总额
```

Binance 官方文档说明：`!forceOrder@arr` 对每个 symbol 每 1000ms 只推送最大一笔强平快照，所以强平指标应命名为 `liquidation_snapshot_*`，避免误解成总量。

### 暂缓接入

MVP 暂不接：

```text
现货数据
全量 aggTrade
depth@100ms
盘口本地 order book
大户多空账户比
复杂回测 UI
```

这些放到第二阶段，等基础告警质量稳定后再加。

## 4. 存储模型

核心表使用 TimescaleDB hypertable。

### symbols

```sql
symbols (
  exchange,
  market_type,
  symbol,
  base_asset,
  quote_asset,
  status,
  tick_size,
  step_size,
  min_notional,
  tier,
  is_active,
  updated_at
)
```

### futures_kline_1m

```sql
futures_kline_1m (
  ts,
  symbol,
  open,
  high,
  low,
  close,
  base_volume,
  quote_volume,
  trade_count,
  taker_buy_base_volume,
  taker_buy_quote_volume,
  created_at,
  primary key (ts, symbol)
)
```

### futures_open_interest

```sql
futures_open_interest (
  ts,
  symbol,
  open_interest,
  open_interest_value,
  created_at,
  primary key (ts, symbol)
)
```

### futures_mark_price

```sql
futures_mark_price (
  ts,
  symbol,
  mark_price,
  index_price,
  funding_rate,
  next_funding_time,
  created_at,
  primary key (ts, symbol)
)
```

### liquidation_snapshots

```sql
liquidation_snapshots (
  id,
  ts,
  symbol,
  side,
  price,
  average_price,
  quantity,
  quote_value,
  raw,
  created_at
)
```

不要用 `(ts, symbol, side, price)` 当主键，强平事件可能同时间同价格碰撞。

### market_factor_1m

用于市场共振过滤：

```sql
market_factor_1m (
  ts,
  btc_return_1m,
  eth_return_1m,
  market_median_return_1m,
  market_dispersion_1m,
  created_at,
  primary key (ts)
)
```

### indicator_snapshot_1m

```sql
indicator_snapshot_1m (
  ts,
  symbol,

  return_1m,
  return_5m,
  return_15m,

  btc_relative_return_1m,
  beta_adjusted_return_1m,

  quote_volume_1m,
  volume_percentile,
  volume_robust_z,

  taker_buy_ratio,
  taker_sell_ratio,
  candle_body_ratio,
  candle_range_bps,

  oi_change_5m,
  oi_change_15m,
  oi_robust_z,

  funding_rate,
  funding_percentile,

  price_spike_score,
  flat_oi_buildup_score,

  created_at,
  primary key (ts, symbol)
)
```

### alerts

```sql
alerts (
  id,
  ts,
  symbol,
  alert_type,
  severity,
  direction,
  state,
  score,
  title,
  message,
  payload,
  mode,
  delivery_status,
  discord_sent_at,
  parent_alert_id,
  created_at
)
```

`state` 使用：

```text
open
escalated
resolved
expired
```

`delivery_status` 使用：

```text
shadow
pending
sent
failed
rate_limited
suppressed
```

### alert_cooldowns

```sql
alert_cooldowns (
  key,
  last_sent_at,
  last_score,
  count_1h,
  updated_at
)
```

## 5. 指标与信号设计

### 市场共振过滤

这是 Day 1 必须做的能力。

普通收益：

```text
return_1m = close / previous_close - 1
```

市场相对收益：

```text
btc_relative_return_1m = symbol_return_1m - beta * btc_return_1m
```

MVP 可以先用简化版：

```text
beta = 1
btc_relative_return_1m = symbol_return_1m - btc_return_1m
```

后续再滚动估计 beta。

如果 BTC 同时剧烈波动，系统不应该对 200 个币分别刷屏，而应该生成一条市场级 digest：

```text
市场共振：BTC 1m +1.4%，全市场同步上行，Top movers: SOL, DOGE, LINK...
```

### 量能异常

优先使用 percentile / robust z，不依赖正态分布：

```text
volume_percentile_7d
volume_percentile_30d
volume_robust_z = 0.6745 * (log_volume - median) / MAD
```

触发建议：

```text
volume_percentile_7d >= 0.99
或
volume_robust_z >= 4
```

### 主动买卖方向

先使用 kline 自带字段：

```text
taker_buy_ratio = taker_buy_quote_volume / quote_volume
taker_sell_ratio = 1 - taker_buy_ratio
```

MVP 不需要落全量 aggTrade。

### 上行主动买入冲击

触发条件：

```text
btc_relative_return_1m > price_threshold
volume_percentile_7d >= 0.99
taker_buy_ratio >= 0.75
```

方向：

```text
up
```

等级：

```text
WARNING：满足三因子
CRITICAL：同时 OI 15m 明显上升，或连续 2-3 分钟升级
```

### 下行主动卖出冲击

触发条件：

```text
btc_relative_return_1m < -price_threshold
volume_percentile_7d >= 0.99
taker_buy_ratio <= 0.25
```

方向：

```text
down
```

### 买盘 / 卖盘被吸收

避免只看 close 没动导致误判，需要加入 K 线结构。

买盘被吸收：

```text
taker_buy_ratio >= 0.75
volume_percentile_7d >= 0.99
abs(btc_relative_return_1m) < small_move_threshold
candle_body_ratio <= 0.35
candle_range_bps 不极端放大
```

卖盘被吸收：

```text
taker_buy_ratio <= 0.25
volume_percentile_7d >= 0.99
abs(btc_relative_return_1m) < small_move_threshold
candle_body_ratio <= 0.35
candle_range_bps 不极端放大
```

### 横盘增仓

不要用固定 `0.5%` 作为价格变化 floor，改成波动率归一化。

```text
price_move_norm_15m = abs(price_change_15m) / rolling_mad_return_15m
oi_move_norm_15m = oi_change_15m / rolling_mad_oi_change_15m
```

触发：

```text
oi_move_norm_15m >= 2
price_move_norm_15m <= 0.5
volume_15m_percentile >= 0.70
```

方向：

```text
neutral
```

解释：

```text
价格相对平静，但 OI 明显堆积，说明杠杆分歧扩大，后续容易方向选择。
```

### 强平确认

强平数据只作为确认信号，不作为 MVP 主触发信号。

```text
liquidation_snapshot_quote_1m_percentile >= 0.99
价格同向异动
OI 同向下降
```

命名必须使用：

```text
强平快照放大
```

而不是：

```text
真实强平总额放大
```

## 6. 告警系统

### Discord 限流

Discord 官方要求客户端根据 rate limit headers 和 429 的 `retry_after` 处理限流。系统必须有：

```text
token bucket
发送队列
优先级队列
429 retry_after 重试
critical 优先
普通告警可合并 / 丢弃
市场共振时 digest 聚合
```

### 去重与聚合

规则：

```text
同 symbol + alert_type：5 分钟最多发送 1 条
同 symbol 多个 alert_type：30 秒内聚合成一条
市场共振：同一分钟超过 N 个 symbol 触发时，不逐条发，改发 Top 10 digest
CRITICAL：可绕过普通冷却，但 1 分钟同 symbol 最多 1 条
```

### Discord embed 内容

每条告警必须包含：

```text
symbol
方向
等级
触发条件
核心指标
市场背景 BTC return
解释文案
复盘 payload id
```

## 7. 阶段计划

### Phase 0：项目骨架

交付：

```text
Docker Compose
TimescaleDB
Python app skeleton
配置系统
日志系统
Alembic migration
Discord webhook 测试
ALERT_MODE=shadow/live
```

验收：

```text
服务可启动
数据库可连接
migration 可执行
shadow/live 配置生效
Discord 测试消息可发送
```

### Phase 1：基础行情采集

范围：

```text
10 个高流动性 USDT 永续
kline_1m
openInterest 60s REST
markPrice / funding
exchangeInfo
```

验收：

```text
连续运行 24 小时不中断
WebSocket 断线可重连
只入库 x=true 的 closed kline
K 线缺口率 < 0.1%
upsert 幂等
```

### Phase 2：基础指标与 baseline

交付：

```text
return_1m / 5m / 15m
BTC-relative return
volume percentile
volume robust z
taker buy/sell ratio
OI change
rolling MAD baseline
```

验收：

```text
每分钟生成 indicator_snapshot_1m
指标延迟 < 10 秒
抽样 20 条和 SQL 手算一致
新币 warmup 不触发 live 告警
```

### Phase 3：Shadow 信号

交付：

```text
上行主动买入冲击
下行主动卖出冲击
买盘被吸收
卖盘被吸收
横盘增仓
```

验收：

```text
shadow 跑 3-7 天
告警只入库不发 Discord
可按 symbol / alert_type / severity 查询
人工复盘后能调整阈值
```

### Phase 4：Live 告警

交付：

```text
Discord embed
token bucket
rate limit header 处理
去重
聚合
市场共振 digest
状态机 open/escalated/resolved
```

验收：

```text
Discord 不刷屏
429 会根据 retry_after 重试
CRITICAL 优先发送
普通告警可合并
alerts 表能完整复盘
```

### Phase 5：扩展合约上下文

交付：

```text
forceOrder snapshot 入库
强平快照确认信号
funding percentile
OI 横盘堆积增强条件
```

验收：

```text
强平信号文案明确标注“采样快照”
不会把 forceOrder 当真实总量
组合信号能解释价格、量能、主动方向、OI、funding 的关系
```

## 8. MVP 暂不做

```text
现货数据
全量 aggTrade 落库
depth@100ms
本地 order book
链上数据
自动下单
用户私有账户数据
复杂机器学习
Web 回测页面
Kafka / Redpanda
多交易所套利
```

这些不是不要，而是等 MVP 信号质量和系统稳定性验证后再加。

## 9. 验收标准

### 稳定性

```text
单进程 MVP 连续运行 7 天
WebSocket 自动重连
REST 限速处理
数据库写入幂等
日志不会无限增长
重启后可继续运行
```

### 数据质量

```text
closed kline 无明显缺口
OI 轮询稳定
时间戳统一使用交易所时间
baseline 有 warmup 机制
新 symbol 不因缺历史数据误报
```

### 信号质量

```text
能识别真实拉盘 / 砸盘
能过滤 BTC 带动的全市场噪声
能识别放量但价格不动的吸收
能识别横盘增仓
每条告警有解释和 payload
```

### 告警质量

```text
默认 shadow 起步
live 模式不刷屏
Discord 429 可恢复
市场共振时发 digest
CRITICAL 不被普通冷却误伤
```

## 10. 默认假设

```text
当前仓库为空或接近空仓库，可以按新项目骨架实施
第一版以 Binance USD-M Futures 为主
数据库使用 TimescaleDB，而不是 vanilla PostgreSQL 分区表
MVP 先监控 10 个高流动性 USDT 永续
默认先 shadow 运行 3-7 天再切 live
forceOrder 只作为采样快照，不作为真实强平总量
```

## 11. 参考依据

- Binance Spot WebSocket 文档说明单连接 stream 上限、24h 断连、ping/pong、market-data-only endpoint 等机制：<https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams>
- Binance USD-M Futures kline 文档说明 `x` 字段表示 K 线是否收盘：<https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Kline-Candlestick-Streams>
- Binance USD-M Futures `!forceOrder@arr` 文档说明每个 symbol 每 1000ms 只推送最大一笔强平快照：<https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Liquidation-Order-Streams>
- Discord rate limit 文档要求根据 `X-RateLimit-*` headers 和 429 `retry_after` 处理限流：<https://docs.discord.com/developers/topics/rate-limits>
- TimescaleDB hypertable 文档说明 hypertable 按时间 chunk 分区，并保持普通 PostgreSQL 表的使用方式：<https://www.tigerdata.com/docs/use-timescale/latest/hypertables>
