# Maker/Post-Only 进场 — 实现 + 前向 A/B 方案

## 为什么是它(最高杠杆)
- 实盘 27 天 16,550 笔成交 **100% taker**(`maker` 列全 False),手续费 −$837。
- Taker 0.04% → Maker 0.02%(或返佣)。**不减交易次数**就能砍掉 ~一半进场端手续费。
- 毛边际 ≈ 0.022%/名义 ≈ 一档 maker 省下的费。把成本压到约等于毛边际 → **拉回打平附近**。
  (注:这只解决"费>毛"的机械亏损,造真 edge 是另一件事。)

## 核心矛盾:成交确定性 vs 手续费
现状 `_open_position`(strategy.py:514)在扫描时(:00:02)用 **MARKET** 单立即成交,
然后挂 STOP_MARKET 止损。Post-only 限价单**可能挂不上**——若挂不上就错过这根 1H 突破。
所以设计的全部难点是"挂不上怎么办",这也是**必须前向测、不能回测定论**的原因。

三种未成交处理策略(A/B 要测的就是它):
| 策略 | 未成交时 | 利 | 弊 |
|---|---|---|---|
| P1 cancel-skip | 撤单跳过该信号 | 永远拿 maker 费;不追价 | 漏掉真突破(机会成本未知) |
| P2 chase-once | 撤单后用新 best 价再挂一次,仍不成交则跳过 | 提高成交率 | 仍可能漏 |
| P3 taker-fallback | 等 N 秒未成交 → 转 MARKET | 不漏单 | 退化回 taker,省费打折扣 |

## 最小改动实现草图

**config.py** 加开关:
```python
ENTRY_ORDER_TYPE: str = "taker"      # "taker"(现状) | "maker"
MAKER_OFFSET_TICKS: int = 1          # 挂在 best bid/ask 内侧几个 tick(post-only)
MAKER_FILL_TIMEOUT_S: int = 45       # 等待成交秒数(< 下一次 risk check)
MAKER_UNFILLED_POLICY: str = "cancel_skip"  # cancel_skip | chase_once | taker_fallback
```

**exchange.py** 加方法(对称现有 place_order):
```python
def place_maker_order(self, symbol, side, quantity, position_side=None):
    """Post-only LIMIT(timeInForce=GTX). 被动挂单;若会立即成交,交易所直接拒单 → 我们当未成交处理。"""
    book = self._retry(lambda: self.data_client.futures_orderbook_ticker(symbol=symbol))
    bid, ask = float(book["bidPrice"]), float(book["askPrice"])
    tick = self.get_tick_size(symbol)   # 已有 round_price 的 tick 逻辑,抽出来
    px = (bid + config.MAKER_OFFSET_TICKS*tick) if side=="BUY" else (ask - config.MAKER_OFFSET_TICKS*tick)
    px = self.round_price(symbol, px)
    params = dict(symbol=symbol, side=side, type="LIMIT", timeInForce="GTX",
                  quantity=quantity, price=px)
    if self._is_hedge_mode(): params["positionSide"] = position_side or "BOTH"
    return self._retry(lambda: self.trading_client.futures_create_order(**params))
    # GTX = post-only:若会吃单,Binance 返回 -2021/立即 EXPIRED → 视为未成交
```

**strategy.py `_open_position`**:在 514 行分叉
```python
if config.ENTRY_ORDER_TYPE == "maker":
    order = exchange.place_maker_order(symbol, order_side, quantity, position_side=side)
    filled = _await_fill(exchange, symbol, order["orderId"], config.MAKER_FILL_TIMEOUT_S)
    if not filled:
        order = _handle_unfilled(...)   # 按 MAKER_UNFILLED_POLICY
        if order is None:
            log_skip(); return          # P1/P2 放弃 → 不开仓,干净返回
# 之后的 fill_price/止损挂单逻辑不变(用实际成交价)
```
**关键**:止损单依赖真实 `fill_price`,所以必须等到 maker 单 FILLED 再挂 SL —— 现有
`get_order_fill` 已能拿成交价,顺序不变。部分成交按 `executed_qty` 挂等量 SL(现有逻辑已兼容)。

## A/B 方法论(单账户,不能同时跑两组)
单账户无法真并行,用**时间切片轮换**消除行情偏差:
- 按天(或按扫描小时)奇偶切 `ENTRY_ORDER_TYPE`:偶数小时 maker、奇数小时 taker。
- 跑 ≥ 2 周(覆盖涨/跌/震荡),每组 ≥ ~150 单。
- 比 maker 的三种 unfilled policy:先固定 P3(taker_fallback,零漏单)跑出**成交率**和
  **实测 maker 占比**,再决定要不要切到 P1(更省费但漏单)。

## 要记的指标(进场单粒度,落到日志/CSV)
- `maker_fill_rate`:post-only 单成交占比(挂上的 / 尝试的)。
- `realized_maker_pct`:成交里真 maker 的占比(对照现状 0%)。
- `fee_per_notional`:两组对比(目标 taker 0.04% → maker 接近 0.02%)。
- `missed_entry_cost`:P1/P2 跳过的信号,用 1m 回放算"若按 taker 进会盈亏多少"
   —— 这是 maker 省费的**对冲成本**,决定净收益符号(`scripts/replay_*` 已有路径回放可复用)。

## 决策规则
maker 组 **净 PnL(含省下的费 − 漏单机会成本) > taker 组**,且统计上不在噪声内 → 切 maker。
否则保留 taker_fallback(P3)作为"零漏单 + 部分省费"的安全档。

## 风险/坑
- GTX 被拒(会吃单)要当未成交、不可重试成 taker(否则偷偷变 taker)。
- 部分成交:SL 挂 `executed_qty`,剩余撤单。
- 挂单期间价格跑掉 → 这正是 missed_entry_cost,如实记。
- 不要和 `TRAILING_ACTIVATION 0.03→0.035` 实验同窗叠加 —— 先让那个出结果,或分开切片。
