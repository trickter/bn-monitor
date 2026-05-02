# 告警策略说明

本文档描述 `bn-monitor` 当前实现的 Binance USD-M 永续市场监控策略，以代码为准。当前版本已经支持固定 symbol MVP 和全 USDT 永续扩容模式，并加入了市场中位数降噪、横盘增仓抑制、Discord 格式化和数据保留任务。

## 数据范围

默认仍使用 `.env` 中的 `SYMBOLS` 固定列表。若设置：

```env
UNIVERSE_MODE=all_usdt_perpetual
```

启动时会通过 Binance `exchangeInfo` 同步 universe，只纳入：

- `quoteAsset == USDT`
- `status == TRADING`
- `contractType == PERPETUAL`
- 24h ticker `quoteVolume >= MIN_24H_QUOTE_VOLUME_USD`，默认 5,000,000
- 不在 `EXCLUDED_SYMBOLS` 手动黑名单中

`configured` 模式不应用 24h 流动性 gate，只使用显式配置的 `SYMBOLS`。

Universe 仅在启动时同步一次。新上市合约不会在进程运行中自动加入；已下架或异常合约也不会自动移除，直到下次重启或进入 `EXCLUDED_SYMBOLS`。

## 数据采集

| 来源 | 数据 | 频率 | 落库表 |
|---|---|---:|---|
| WS `/market/stream` `<symbol>@kline_1m` | 1 分钟闭合 K 线 OHLCV、taker buy 量、笔数 | 实时，仅 `x=true` 入库 | `futures_kline_1m` |
| WS `/market/stream` `!markPrice@arr@1s` | mark/index/funding/next_funding | 1s 全市场推送 | `futures_mark_price` |
| WS `/market/stream` `!forceOrder@arr` | 强平快照 | 实时，Binance 采样流，非全量 | `liquidation_snapshots` |
| REST `/fapi/v1/ticker/24hr` | 24h quoteVolume | 启动时 all-universe 模式使用 | 不落库 |
| REST `/fapi/v1/exchangeInfo` | 合约元数据 | 启动和 REST 轮询 | `symbols` |
| REST `/fapi/v1/premiumIndex` | mark/index/funding 兜底 | 默认 60s | `futures_mark_price` |
| REST `/futures/data/openInterestHist` | 5m OI 历史统计 | 默认 300s | `futures_open_interest` |
| REST `/fapi/v1/openInterest` | 当前 OI fallback | OI history 失败或空数据时 | `futures_open_interest` |
| REST `/fapi/v1/klines` | 启动回补最近闭合 K 线 | 启动第一轮 | `futures_kline_1m` |
| REST `/fapi/v1/fundingRate` | funding 历史 seed | 启动成功一次 | `futures_mark_price` |

WS 使用 Binance routed endpoint。常规市场数据走 `wss://fstream.binance.com/market/stream?...`。K 线按 `WS_KLINE_STREAM_CHUNK_SIZE` 拆分连接，默认 300；markPrice 和 forceOrder 订阅单独一条连接。

未收盘 K 线不会入库，也不会参与计算。WS 检查 kline `x=true`，REST K 线检查 `close_time < now_ms`。

## 指标计算

`compute_latest_indicators` 默认每 5 秒触发一次。当前实现不再要求所有 symbol 拥有同一个最新分钟，而是每个 symbol 取自己未过期的最新闭合 K 线：

- 查询每个 symbol 的 `max(ts)` 等价最新行
- 过滤 `ts >= now - MARKET_DATA_MAX_STALENESS_MINUTES`
- 缺最新 K 线的 symbol 跳过，不阻塞其他 symbol

### 收益类

- `return_1m / return_5m / return_15m = close / close[t-N] - 1`
- `btc_relative_return_1m = return_1m - BTC.return_1m`
- `market_relative_return_1m = return_1m - market_median_return_1m`
- `price_spike_score`：
  - 普通 alt：`min(abs(btc_relative), abs(market_relative))`
  - BTC/ETH：`abs(market_relative)`
- `price_move_norm_15m = abs(return_15m) / max(MAD(return_15m_sample), NORMALIZED_MOVE_MIN_MAD_BPS)`

`NORMALIZED_MOVE_MIN_MAD_BPS` 默认 1 bps，用来避免 MAD 极小时归一化指标爆炸。

### 量能类

基线为同 symbol 过去 7 天 1m 样本，最多 10080 行：

- `volume_percentile = #{x <= current} / #sample`
- `volume_robust_z = 0.6745 * (current - median) / MAD`
- `taker_buy_ratio = taker_buy_quote_volume / quote_volume`
- `taker_sell_ratio = 1 - taker_buy_ratio`
- `candle_body_ratio = abs(close - open) / (high - low)`
- `candle_range_bps = (high - low) / close * 10000`

样本少于 5 时 percentile/robust z 返回 `None`，相关告警自然不会触发。这也是新上市币种第一天降噪的基础保护。

### 持仓 / 杠杆类

OI 基线窗口为同 symbol 最近 240 条 OI snapshot：

- `oi_change_5m / oi_change_15m`：按时间差寻找至少 300s / 900s 之前的最近 snapshot 配对
- `oi_change_sample`：用相同时间窗口生成历史 15m OI 变化序列
- `oi_robust_z`：`oi_change_15m` 在样本中的 robust z
- `oi_move_norm_15m = oi_change_15m / max(MAD(sample), NORMALIZED_MOVE_MIN_MAD_BPS)`，保留方向
- `flat_oi_buildup_score = oi_change_15m * 10000`

OI history 接口是 5m 粒度，适合作为大 universe 下的低成本压力控制。若接口失败或返回空数据，会 fallback 到当前 OI 接口，并用 mark price 估算 `open_interest_value`。

### 资金费率

- `funding_rate`：最新 funding
- `funding_percentile`：当前 funding 在过去 7 天 funding 样本中的位置

### 市场因子

`market_factor_1m` 每轮指标计算写入：

- `btc_return_1m`
- `eth_return_1m`
- `market_median_return_1m`
- `market_dispersion_1m`

市场因子是 450 币种降噪的核心。普通 alt 的方向性告警要求 BTC-relative 和 market-median-relative 同时越线；任一缺失时，该分钟不 fallback 到单门槛。

## 告警规则

阈值默认值见 `bn_monitor/config.py`，均可用 `.env` 覆盖。

### 1. `active_buy_impulse`：主动买入冲击

触发条件：

- 普通 alt：`btc_relative_return_1m_bps > PRICE_THRESHOLD_BPS` 且 `market_relative_return_1m_bps > PRICE_THRESHOLD_BPS`
- BTC/ETH：`market_relative_return_1m_bps > PRICE_THRESHOLD_BPS`
- `volume_percentile >= VOLUME_PERCENTILE_THRESHOLD` 或 `volume_robust_z >= VOLUME_ROBUST_Z_THRESHOLD`
- `taker_buy_ratio >= 0.75`

严重度：

- `oi_robust_z >= OI_BUILDUP_THRESHOLD` 为 `CRITICAL`
- 否则为 `WARNING`

含义：价格同时跑赢 BTC 和市场中位数，且有异常量能和主动买盘。对 BTC/ETH 自身，BTC-relative 不适用，因此走 market-relative 单门槛。

### 2. `active_sell_impulse`：主动卖出冲击

触发条件是 `active_buy_impulse` 的镜像：

- 普通 alt：两个相对收益都 `< -PRICE_THRESHOLD_BPS`
- BTC/ETH：`market_relative_return_1m_bps < -PRICE_THRESHOLD_BPS`
- volume 条件同上
- `taker_buy_ratio <= 0.25`

严重度同上。

### 3. `buy_absorption`：买盘吸收

触发条件：

- 普通 alt：`abs(btc_relative_bps) < SMALL_MOVE_THRESHOLD_BPS` 且 `abs(market_relative_bps) < SMALL_MOVE_THRESHOLD_BPS`
- BTC/ETH：`abs(market_relative_bps) < SMALL_MOVE_THRESHOLD_BPS`
- absorption 专用 volume 条件：`volume_percentile >= ABSORPTION_VOLUME_PERCENTILE_THRESHOLD` 且 `volume_robust_z >= ABSORPTION_VOLUME_ROBUST_Z_THRESHOLD`
- `candle_body_ratio <= 0.35`
- `candle_range_bps <= 200`
- `taker_buy_ratio >= 0.75`

严重度固定为 `WARNING`。默认 per-type 冷却为 60 分钟，避免横盘阶段同类吸收信号反复推送。

含义：大量主动买入没有推动价格明显上涨，可能有被动卖压吸收。

### 4. `sell_absorption`：卖盘吸收

与 `buy_absorption` 镜像，区别是 `taker_buy_ratio <= 0.25`。

### 5. `flat_oi_buildup`：横盘增仓

触发条件：

- `oi_move_norm_15m >= OI_BUILDUP_THRESHOLD`
- `oi_change_15m * 10000 >= FLAT_OI_MIN_OI_CHANGE_BPS`，默认 150 bps
- `price_move_norm_15m <= PRICE_FLAT_NORM_THRESHOLD`
- `volume_percentile >= FLAT_OI_VOLUME_PERCENTILE_THRESHOLD`，默认 0.90

严重度固定为 `WARNING`。

含义：价格波动率标准化后仍然横盘，但 OI 明显增加，说明杠杆仓位堆积。该信号只提示杠杆分歧，不直接给方向。默认 per-type 冷却为 60 分钟，避免横盘增仓刷屏。

### 6. `liquidation_snapshot_confirmation`：强平快照确认

按 `minute + symbol + side` 聚合 `liquidation_snapshots.quote_value`。

触发条件：

- 当前分钟聚合值 >= 同 symbol/side 过去 7 天每分钟样本的 99 分位
- 价格同向：BUY 强平且 `btc_relative_return_1m > 0`，或 SELL 强平且 `< 0`
- `oi_change_15m < 0`

严重度固定为 `WARNING`。

这是滞后确认信号，不是先行信号。它用于在已有趋势中标记“强平放大”的瞬间。

## Live 模式聚合与抑制

仅 `ALERT_MODE=live` 生效；shadow 模式保留所有原子告警，便于回放和调参。

### `symbol_alert_bundle`

同一 symbol 在 30 秒滚动窗口内触发多条原子告警时合并为一条：

- severity 取最高
- `payload.merged_alert_types` 列出原子类型
- `payload.component_scores` 保留各自分数

### `market_digest`

同一分钟内触发 symbol 数量达到 `DIGEST_TRIGGER_COUNT`，默认 5，会输出一条 `symbol=MARKET` 的市场摘要，并替换该分钟的个体推送。目的是 BTC 大波动或系统性行情时避免刷屏。

### 每轮 live 上限

`MAX_LIVE_ALERTS_PER_CYCLE` 默认 5。超过上限的 live 告警仍入库，但 `delivery_status=suppressed`，不推送 Discord。

## 状态机

每条 alert 有：

- `mode in {shadow, live}`
- `state in {open, escalated, resolved, expired}`
- `delivery_status in {pending, sent, failed, rate_limited, suppressed, shadow}`

状态转移：

| 事件 | 转移 |
|---|---|
| 新触发，过去 15 分钟内有同 `(symbol, alert_type, mode)` 的 `open/escalated` | `state=escalated`，写 `parent_alert_id` |
| 新触发，无前序 active alert | `state=open` |
| 当前周期 active_keys 中没有 `(symbol, alert_type)` | 旧 `open/escalated` 转为 `resolved` |
| 6 小时未关闭 | `state=expired` |
| live 且冷却期内 | 新 alert 入库，`delivery_status=suppressed`，不推送 Discord |

CRITICAL escalation 不会被同 type 冷却挡住：若已有同 signal 的非 CRITICAL 父告警，新 CRITICAL 可绕过冷却完成升级推送。

## 冷却

冷却 key 为：

```text
{symbol}:{alert_type}
```

默认配置：

| key | 默认冷却 |
|---|---:|
| `CRITICAL` | 5 分钟 |
| `WARNING` | 10 分钟 |
| `flat_oi_buildup` | 60 分钟 |

可通过 `ALERT_COOLDOWN_MINUTES` JSON 覆盖，例如：

```env
ALERT_COOLDOWN_MINUTES={"CRITICAL":5,"WARNING":10,"buy_absorption":60,"sell_absorption":60,"flat_oi_buildup":60}
```

冷却使用双层缓存：进程内 dict + 持久化 `alert_cooldowns` 表，重启不丢。`count_1h` 记录 1 小时内滚动次数，可用于后续更复杂的频率衰减。

## Discord 投递

Discord webhook 使用内存 token bucket，默认 capacity=5、refill=1/s。

- 200/204：`delivery_status=sent`；如果响应头 `X-RateLimit-Remaining=0`，按 `X-RateLimit-Reset-After` 等待
- 429：解析 `Retry-After` 或响应 JSON `retry_after`，最多重试 3 次；最终失败为 `rate_limited`
- 5xx：指数退避重试
- 其他 4xx：直接 `failed`

Embed 已中文化并保留英文小字段，便于回查代码：

- `交易对 (symbol)`
- `方向 (direction)`
- `级别 (severity)`
- `触发条件 (conditions)`
- `核心指标 (metrics)`：volume percentile、volume z、taker buy、OI z、OI norm、OI bps、funding
- `市场背景 (context)`：BTC return、BTC-relative、market median、market-relative
- `Payload ID`：包含 `bn-monitor alert-show <id>` 复盘命令提示

颜色优先按 alert type：横盘增仓灰色，买入冲击/买盘吸收绿色，卖出冲击/卖盘吸收红色，强平紫色，market digest 黄色，多信号 bundle 蓝色。

## CLI 辅助

| 命令 | 用途 |
|---|---|
| `bn-monitor config-dump` | 查看当前配置，敏感 URL 脱敏 |
| `bn-monitor compute-indicators` | 手动计算最新指标 |
| `bn-monitor generate-alerts` | 手动生成最新告警 |
| `bn-monitor alert-projection --hours 48 --profile balanced` | 用历史 `indicator_snapshot_1m` 重放当前规则，估算触发量 |
| `bn-monitor alert-show <id>` | 按 payload id 回看 alert |
| `bn-monitor data-quality --lookback-hours 24` | 检查 K 线缺口和 mark/OI 新鲜度 |
| `bn-monitor alert-summary --lookback-hours 24` | 汇总告警分布 |
| `bn-monitor retention-run` | 删除超过 `DATA_RETENTION_DAYS` 的大体量时序数据 |

`retention-run` 默认清理：

- `futures_kline_1m`
- `futures_open_interest`
- `futures_mark_price`
- `liquidation_snapshots`
- `market_factor_1m`
- `indicator_snapshot_1m`

不会清理 `symbols`、`alert_cooldowns` 和 `alerts`。

## 配置参数

| key | 默认 | 含义 |
|---|---:|---|
| `UNIVERSE_MODE` | `configured` | `configured` 或 `all_usdt_perpetual` |
| `MIN_24H_QUOTE_VOLUME_USD` | 5000000 | all-universe 模式的 24h quote volume 下限 |
| `EXCLUDED_SYMBOLS` | 空 | 手动排除列表 |
| `PRICE_THRESHOLD_BPS` | 35 | active impulse 相对收益阈值 |
| `SMALL_MOVE_THRESHOLD_BPS` | 10 | absorption 的小幅波动阈值 |
| `VOLUME_PERCENTILE_THRESHOLD` | 0.99 | 量能异常 percentile |
| `VOLUME_ROBUST_Z_THRESHOLD` | 4 | 量能异常 robust z 备选 |
| `ABSORPTION_VOLUME_PERCENTILE_THRESHOLD` | 0.99 | absorption 专用成交量分位阈值 |
| `ABSORPTION_VOLUME_ROBUST_Z_THRESHOLD` | 6 | absorption 专用 robust z 阈值 |
| `OI_BUILDUP_THRESHOLD` | 2 | OI z 或归一化 OI move 阈值 |
| `PRICE_FLAT_NORM_THRESHOLD` | 0.5 | flat OI 的价格横盘阈值 |
| `FLAT_OI_VOLUME_PERCENTILE_THRESHOLD` | 0.90 | flat OI 的成交量分位阈值 |
| `FLAT_OI_MIN_OI_CHANGE_BPS` | 150 | flat OI 的 15m OI 绝对变化下限 |
| `NORMALIZED_MOVE_MIN_MAD_BPS` | 1 | `normalized_move` MAD floor |
| `DIGEST_TRIGGER_COUNT` | 5 | market digest 的同分钟 symbol 数阈值 |
| `MAX_LIVE_ALERTS_PER_CYCLE` | 5 | 每轮 live 推送上限 |
| `ALERT_COOLDOWN_MINUTES` | JSON | severity/type 冷却配置 |
| `REST_POLL_INTERVAL_SECONDS` | 60 | REST public context 轮询周期 |
| `REST_MAX_REQUESTS_PER_SECOND` | 15 | REST 客户端最小间隔限速 |
| `OPEN_INTEREST_POLL_INTERVAL_SECONDS` | 300 | OI history 轮询周期 |
| `INDICATOR_POLL_INTERVAL_SECONDS` | 5 | 指标重算周期 |
| `WS_KLINE_STREAM_CHUNK_SIZE` | 300 | K 线 WS 每连接 symbol 数 |
| `WS_FLUSH_INTERVAL_SECONDS` | 0.2 | WS 批量入库刷新间隔 |
| `KLINE_BACKFILL_LIMIT` | 180 | 启动 K 线回补数量 |
| `DATA_RETENTION_DAYS` | 30 | 时序数据保留天数 |
| `ALERT_MODE` | `shadow` | `shadow` 只入库；`live` 入库并推送 |

## 设计选择

| 维度 | 选择 | 理由 |
|---|---|---|
| 时间粒度 | 1 分钟闭合 K 线 | 控制噪声，避免未完成 K 线反复变化 |
| universe | 固定列表或启动时全 USDT 永续同步 | 保留 MVP 可控性，同时支持扩容 |
| 流动性 gate | all-universe 模式下用 24h quoteVolume | 避免僵尸合约触发假信号 |
| 市场剥离 | BTC-relative + market-median-relative 双门槛 | 避免 BTC 系统性波动污染个体告警 |
| BTC/ETH 特例 | 走 market-relative 单门槛 | BTC 相对 BTC 恒为 0，不能用普通双门槛 |
| 基线 | 7 天 MAD / percentile | 稳健，不假设正态分布 |
| OI | confirmation/divergence，不单独做方向主信号 | OI 有滞后且可能受采样粒度影响 |
| 强平流 | 仅作 confirmation | Binance forceOrder 是采样流，不是全量强平 |
| live 聚合 | digest/bundle + per-cycle cap | 系统性行情时避免 Discord 刷屏 |
| shadow 优先 | 默认 shadow | 参数先回放评估，再切 live |

## 已知弱点

1. 新上市合约仍缺少长期 baseline。样本少于 5 会返回 `None`，可避免最早期假阳性，但第一天后仍可能较噪。
2. `openInterestHist` 是每 symbol 请求，不是真正一次全市场批量接口；当前通过 5 分钟 cadence 控制在 Binance 限制内。
3. 当前阈值仍是全局统一阈值，没有按 symbol 波动率或成交量 tier 自适应。
4. `market_median_return_1m` 对极端分化行情可能偏慢，后续可考虑分层 median，例如大市值 / 中小市值分组。
5. `retention-run` 是手动 CLI，不是后台 cron；生产环境需要外部定时任务调用。
6. `alerts` 目前不随 30d retention 清理，便于复盘，但长期运行后需要单独归档策略。
7. 强平确认依赖采样流，漏报是预期行为，不应拿它当清算总量统计。
