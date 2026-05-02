# 告警策略说明

本文档描述 `bn-monitor` 当前实现的市场异动检测策略，以源码为准。

## 数据采集

| 来源 | 数据 | 频率 | 落库表 |
|---|---|---|---|
| WS `<symbol>@kline_1m` | 1 分钟 K 线 OHLCV、taker buy 量、笔数 | 实时（仅 `x=true` 时入库） | `futures_kline_1m` |
| WS `!markPrice@arr@1s` | mark/index/funding/next_funding | 1s（全市场推送） | `futures_mark_price` |
| WS `!forceOrder@arr` | 强平单 | 实时（**Binance 采样流，非全量**） | `liquidation_snapshots` |
| REST `/openInterest` | 持仓量 | 60s | `futures_open_interest` |
| REST `/premiumIndex` | mark/index/funding 兜底 | 60s | `futures_mark_price` |
| REST `/klines` | 启动回填最近 180 根 | 60s 增量 | `futures_kline_1m` |
| REST `/fundingRate` | funding 历史（启动一次性 seed） | 启动 | `futures_mark_price`（仅 `funding_rate`/`mark_price` 列） |
| REST `/exchangeInfo` | 合约元数据 | 60s | `symbols` |

**强平流是 Binance 的采样流，不是全量**，策略中只用作"确认信号"，不作主信号。

未收盘的 K 线一律不入库 / 不参与计算（`closed_kline_from_ws` 检查 `x` 标志，REST `klines_1m` 检查 `close_time < now_ms`）。

## 指标层

`compute_latest_indicators` 每 5s 触发一次，仅处理"所有 settings.symbols 都有数据的最近共同分钟"作为横截面对齐时点（`HAVING count(DISTINCT symbol) = :symbol_count`）。

### 收益类
- `return_1m / 5m / 15m`：`close / close[t-N] - 1`
- `btc_relative_return_1m = return_1m - BTC.return_1m`
- `price_move_norm_15m = |return_15m| / MAD(return_15m, 7天 sample)`

### 量能类
基线 = 同 symbol 过去 7 天 1m 样本（最多 10080 行）。
- `volume_percentile`：`#{x ≤ current} / #sample`
- `volume_robust_z = 0.6745 × (current - median) / MAD`（样本不足 5 时返回 None）
- `taker_buy_ratio = taker_buy_quote / quote_volume`
- `candle_body_ratio = |close - open| / (high - low)`
- `candle_range_bps = (high - low) / close × 10000`

### 持仓 / 杠杆类
基线窗口 = 该 symbol 最近 240 条 OI snapshot。
- `oi_change_5m / 15m`：找时间差 ≥ 300s / 900s 的最近 snapshot 配对
- `oi_change_sample`：用同样的"时间窗口配对"算 15m 变化率序列（每 row[i] 配对第一个 ≥ 900s 之前的 row[j]）
- `oi_robust_z`：`oi_change_15m` 在样本里的 robust z
- `oi_move_norm_15m = oi_change_15m / MAD(sample)`（**保留方向**）

### 资金费率
- `funding_rate`：最新值
- `funding_percentile`：在过去 7 天 funding 样本中的位置

### 市场因子表 `market_factor_1m`
- `btc_return_1m`, `eth_return_1m`
- `market_median_return_1m`：横截面收益中位数
- `market_dispersion_1m`：横截面 MAD

## 告警规则

阈值默认值见 `bn_monitor/config.py`，全部可由 `.env` 覆盖。

### 1. `active_buy_impulse` — 主动买盘冲击

**触发（AND）**：
- `btc_relative_return_1m_bps > PRICE_THRESHOLD_BPS`（默认 35）
- `volume_percentile ≥ VOLUME_PERCENTILE_THRESHOLD`（0.99）**或** `volume_robust_z ≥ VOLUME_ROBUST_Z_THRESHOLD`（4）
- `taker_buy_ratio ≥ 0.75`

**严重度**：`oi_robust_z ≥ OI_BUILDUP_THRESHOLD`（2）→ CRITICAL，否则 WARNING。

**含义**：跑赢 BTC + 量能异常 + 主动买为主，伴随 OI 增加视为新多入场。

### 2. `active_sell_impulse` — 镜像

- `btc_relative_return_1m_bps < -PRICE_THRESHOLD_BPS`
- 同样的 volume 条件
- `taker_buy_ratio ≤ 0.25`
- 严重度逻辑同上

### 3. `buy_absorption` — 买盘被吸收

**触发（AND）**：
- `|btc_relative_return_1m_bps| < SMALL_MOVE_THRESHOLD_BPS`（10）
- volume 异常（同上）
- `candle_body_ratio ≤ 0.35`
- `candle_range_bps ≤ 200`
- `taker_buy_ratio ≥ 0.75`

**严重度**：WARNING。

**含义**：大量主动买未推动价格 → 有大单卖压被动接盘。

### 4. `sell_absorption` — 镜像（`taker_buy_ratio ≤ 0.25`）

### 5. `flat_oi_buildup` — 杠杆背离

**触发（AND）**：
- `oi_move_norm_15m ≥ OI_BUILDUP_THRESHOLD`（2，归一化后）
- `price_move_norm_15m ≤ PRICE_FLAT_NORM_THRESHOLD`（0.5，归一化后）
- `volume_percentile ≥ 0.70`

**严重度**：WARNING。

**含义**：持仓在堆而价格平 → 资金堆杠杆 / 多空分歧，后续易触发剧烈走势。

### 6. `liquidation_snapshot_confirmation` — 强平放大确认

按"分钟 + side"聚合 `liquidation_snapshots.quote_value`：

**触发（AND）**：
- 当前分钟聚合值 ≥ 同 symbol/side 过去 7 天每分钟样本的 99 分位
- 价格同向（BUY 强平 + `btc_relative_return_1m > 0` / SELL 强平 + < 0）
- `oi_change_15m < 0`（持仓收缩 → 平仓潮，而非新仓）

**严重度**：WARNING。

这是**滞后确认信号**，不是先行信号；用于在已有趋势中标记"被强平加速"的瞬间。

### 7. live 模式聚合

仅在 `ALERT_MODE=live` 生效，shadow 下保留所有原子告警。

**`symbol_alert_bundle`**：同一 symbol 在 30 秒滚动窗内触发多条 → 合并为一条；severity 取最高，`payload.merged_alert_types` 列出所有原子类型，`payload.component_scores` 保留各自分数。

**`market_digest`**：同一分钟 ≥ `DIGEST_TRIGGER_COUNT`（5）个不同 symbol 触发 → 输出一条 `symbol=MARKET` 行情摘要，**替换**那一分钟的所有个体推送（即不再产出 bundle）。设计目的是系统级行情时不刷屏。

## 状态机

每条 alert 有：
- `mode ∈ {shadow, live}` —— 不会跨 mode 影响
- `state ∈ {open, escalated, resolved, expired, suppressed}`
- `delivery_status ∈ {pending, sent, failed, rate_limited, suppressed, shadow}`

**状态转移**：

| 事件 | 转移 |
|---|---|
| 新触发 + 过去 15min 内有同 (symbol, alert_type, mode) 仍 open/escalated | `state=escalated`, 挂 `parent_alert_id` |
| 新触发 + 无前驱 | `state=open` |
| 当前周期 active_keys 中没有 (symbol, alert_type) | 旧 open/escalated → `state=resolved` |
| 6 小时未关闭 | `state=expired` |
| live + 冷却期内 | 写库为 `state=open`，但 `delivery_status=suppressed`，不推 Discord |

## 冷却

按 `key = "{symbol}:{alert_type}"` 维度：

| severity | 冷却窗口 |
|---|---|
| CRITICAL | 5 分钟 |
| WARNING | 10 分钟 |

双层缓存：进程内 `dict` + 持久化 `alert_cooldowns` 表（重启不丢）。`count_1h` 字段记录 1 小时滚动计数（可用于后续频次衰减/退避，目前未用）。

shadow 模式不走冷却 —— 所有决策都进库，便于回放 / 评估。

## 投递

Discord webhook + 内存 token bucket（默认容量 5、补 1/s，可配）。

- 200/204：`delivery_status=sent`，且若响应头 `X-RateLimit-Remaining=0` 则按 `X-RateLimit-Reset-After` 自我等待
- 429：解析 `Retry-After` / `retry_after` 重试，最多 3 次；最终未成功 → `rate_limited`
- 5xx：指数退避（1, 2, 4s）3 次
- 其他 4xx：直接 `failed`，不再重试

embed 字段：Symbol / Direction / Severity / Trigger Conditions / Core Metrics（vol pct/z, taker buy, OI z, OI norm, funding） / Market Context（BTC return, BTC-relative） / Payload ID。

## 配置参数

| key | 默认 | 含义 |
|---|---|---|
| `PRICE_THRESHOLD_BPS` | 35 | active impulse 的相对收益阈值 |
| `SMALL_MOVE_THRESHOLD_BPS` | 10 | absorption 的"价格几乎没动"阈值 |
| `VOLUME_PERCENTILE_THRESHOLD` | 0.99 | 量能异常 percentile |
| `VOLUME_ROBUST_Z_THRESHOLD` | 4 | 量能异常 robust z 备选 |
| `OI_BUILDUP_THRESHOLD` | 2 | OI 异常阈值（z 或归一化） |
| `PRICE_FLAT_NORM_THRESHOLD` | 0.5 | flat_oi_buildup 的"价格平"阈值 |
| `DIGEST_TRIGGER_COUNT` | 5 | market_digest 触发的同分钟 symbol 数下限 |
| `KLINE_GAP_MAX_RATIO` | 0.001 | 数据质量：允许的 K 线缺口比例 |
| `KLINE_MAX_STALENESS_MINUTES` | 3 | 数据质量：K 线最大滞后分钟 |
| `MARKET_DATA_MAX_STALENESS_MINUTES` | 5 | 数据质量：mark/OI 最大滞后分钟 |
| `INDICATOR_POLL_INTERVAL_SECONDS` | 5 | 指标重算周期 |
| `WS_FLUSH_INTERVAL_SECONDS` | 0.2 | WS 批量入库刷新间隔 |
| `ALERT_MODE` | `shadow` | shadow（仅入库） / live（入库 + 推送） |

## 设计选择

| 维度 | 选择 | 理由 |
|---|---|---|
| 时间粒度 | 1 分钟，仅闭合 K 线 | 噪声可控，上下游一致 |
| 基线 | 同 symbol 过去 7 天 MAD / 百分位 | 鲁棒，不假设正态分布 |
| 市场剥离 | `btc_relative_return`，不用绝对收益 | 避免 BTC beta 污染 |
| 多信号 AND | 每条规则要求"价格 + 量 + 微观结构"同时成立 | 牺牲 recall 换 precision |
| OI 角色 | confirmation / divergence，不作主信号 | OI 自身噪声大，且 60s 采样滞后 |
| 强平流 | 仅作 confirmation | Binance 采样流，不可靠为主信号 |
| live 聚合 | bundle / digest 优先于刷屏 | 行情时段不爆频道 |
| shadow 优先 | 默认 shadow，调参后再 live | 生产推送不可逆，参数先回放评估 |

## 已知弱点

1. **冷启动**：所有 robust z / percentile 要求样本 ≥ 5。新上市 symbol 头几分钟全部返回 None，自然无告警 —— 是有意的，避免基线缺失下的假阳。
2. **BTC 代理失效**：`btc_relative_return` 默认 BTC 是市场代理。山寨季 BTC 自身横盘时，山寨被高估"相对异动"。可以考虑切换到 `market_median_return_1m` 作为参照。
3. **OI 突变误触发**：`oi_robust_z` / `flat_oi_buildup` 在 OI 单点跳变时会假阳。`vol_pct ≥ 0.70` 起到部分缓解。
4. **缺跨周期确认**：所有判定挂在 1m 单根 K 线，没有 5m/15m 独立信号。极短脉冲噪声会被算成事件。
5. **小盘 absorption 假阳**：`taker_buy_ratio` 在低流动性 symbol 极易达到 0.75/0.25 极值，结合小实体会误触发。
6. **冷却仅按 (symbol, alert_type)**：连续不同 alert_type 不会互相抑制。live 下由 `symbol_alert_bundle` 缓解，shadow 下需在分析阶段后处理。
7. **所有阈值是硬编码的统一值**：未做按 symbol 自适应（如波动大的 DOGE 与 BTC 用同一 `PRICE_THRESHOLD_BPS`，明显不合理）。后续可改为按 symbol 历史波动率分位自适应。
