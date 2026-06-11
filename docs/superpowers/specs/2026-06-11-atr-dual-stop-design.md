# 双层 ATR 自适应止损改造设计

日期：2026-06-11
状态：已确认（用户批准）

## 背景与动机

5/6 月实盘逐笔归因（`scripts/analyze_may_june.py`，730 个回合交易）发现：

- 持仓 <1 小时的交易 396 笔净亏 −$4,612（胜率 ~31%，中位持仓 17 分钟）；持仓 ≥1 小时的 345 笔净赚 +$2,887。
- 473 笔亏损中 412 笔（87%）集中在 −2%~−3% 出场——即固定 2% 止损 + 滑点。
- 机制：整点 :01 市价追入 1H 布林带突破（常为局部极值），2% 固定止损埋在高波动币 1H 噪音带内，被分钟级回踩收割。

结论：止损不是在防风险，而是在被噪音收割；一旦扛过第一根 K 线，策略有真实趋势 edge。

## 目标

把止损距离放到噪音带外，同时保持单笔美元风险不变：

1. **软止损**：ATR 自适应距离，仅在 1H 收盘价确认时触发（本地判断），直接对应"扛过第一根 K 线"的数据信号。
2. **硬止损**：更宽的交易所常驻 STOP_MARKET 单，防跳空、防机器人离线。
3. **等风险缩仓**：名义仓位 = 单笔风险额 ÷ 软止损距离，止损放宽多少仓位就缩多少。

非目标：移动止盈侧（激活 3.5% / 回撤 1.5%）本次不动——单变量原则，且它是当前唯一赚钱的部分。

## 设计决策（已确认）

| 决策点 | 选择 |
|---|---|
| 方案范围 | 双层止损（软=ATR+1H收盘确认，硬=交易所宽挂单） |
| 软止损距离 | `max(2%, 1.5 × ATR14(1H) / 入场价)` |
| 硬止损距离 | `min(2 × 软止损, 6%)` |
| 单笔风险额 | $40（与现状 $2,000 名义 × 2% 等效） |
| 止盈侧 | 保持固定 3.5%/1.5% 不变 |
| 上线路径 | 先回测验证（5/6 月数据，fixed vs atr_dual 对比），通过才切实盘 |

## 1. 配置层（config.py 新增）

```python
STOP_MODE: str = "atr_dual"        # "fixed"=旧逻辑(2%交易所止损) | "atr_dual"=新逻辑，可一键回滚
ATR_PERIOD: int = 14               # 1H K线，Wilder ATR，丢最后一根未收盘（项目惯例）
SOFT_STOP_ATR_MULT: float = 1.5
SOFT_STOP_FLOOR_PCT: float = 0.02  # 软止损下限 2%
HARD_STOP_MULT: float = 2.0        # 硬止损 = 软 × 2
HARD_STOP_CAP_PCT: float = 0.06    # 硬止损上限 6%（5x 杠杆下远离爆仓线）
RISK_PER_TRADE_USD: float = 40.0
MAX_NOTIONAL_USD: float = 2000.0   # 名义上限=现状，只缩不放大
```

`FIXED_STOP_LOSS_PCT` 保留，供 `STOP_MODE="fixed"` 回滚使用。

## 2. 开仓流程（strategy.py）

信号确认后、下单前插入计算，复用已拉取的 1H K 线，不增加 API 请求：

- `ATR = risk.calculate_atr(1H K线, period=14)` —— Wilder ATR，新函数放在 `risk.py`，回测引擎复用同一实现。输入丢掉最后一根未收盘 K 线（项目惯例）。
- `软止损% = max(SOFT_STOP_FLOOR_PCT, SOFT_STOP_ATR_MULT × ATR / 入场价)`
- `硬止损% = min(HARD_STOP_MULT × 软止损%, HARD_STOP_CAP_PCT)`
- `名义 = min(RISK_PER_TRADE_USD / 软止损%, MAX_NOTIONAL_USD)`；`保证金 = 名义 / LEVERAGE`；数量按交易所 stepSize 取整。
- 数量低于交易所 minQty / minNotional → 放弃该信号并记日志（不强行下最小单）。
- ATR 数据不足或为 0 → 退化为 floor 值（软 2% / 硬 4%）。
- 交易所 STOP_MARKET 挂在**硬止损价**（替代现在的 2% 位置）。
- 仓位记录新增字段：`soft_stop_pct`、`hard_stop_pct`、`position_size`（该笔实际保证金）、`atr_at_entry`。

## 3. 风控循环（risk.py，每 60 秒）

- **硬止损**：现有交易所挂单检查路径不变，止损价来源从 `config.FIXED_STOP_LOSS_PCT` 改为 `pos["hard_stop_pct"]`（存量仓位无此字段时回退 config 值）。
- **软止损（新增）**：每个整点后的第一个风控 tick（state 中记录 `last_soft_check_hour`，重启安全），对每个持仓拉最近一根**已收盘** 1H K 线收盘价；收盘价越过软止损线 → 市价平仓，原因标 `软止损(1H收盘)`，复用 `_close_position`（自动撤交易所双单，OCO 清理沿用现有逻辑）。
- 入场在 :01，第一次软止损检查发生在下一个整点收盘后 → 天然实现"至少扛过第一根 K 线"。
- **PnL 口径修正**：`calculate_pnl` 的名义兜底分支、`_record_position_close` 中 `update_balance` 与百分比计算，从全局 `config.POSITION_SIZE` 改为 `pos["position_size"]`（等风险缩仓后各笔保证金不同，必须跟改）。存量仓位无该字段时回退 `config.POSITION_SIZE`。
- **存量仓位兼容**：部署时已开仓位按旧逻辑处理（交易所 2% 止损单不动、跳过软止损检查），自然换血。
- 移动止盈路径完全不动。

## 4. 回测验证（切实盘的前置门槛）

- `backtesting/engine.py` 增加同样的 `STOP_MODE` 分支：软止损按 1H 收盘判断；硬止损用 bar 内 high/low 保守模拟（触线按硬止损价成交，跳空越线按该 bar 开盘价成交）。
- 重新下载数据补到 6 月（`download_data.py` 支持续传），用 5/6 月同期回放 `fixed` vs `atr_dual` 对比：净 PnL、手续费总额、<1 根 K 线死亡率、最大回撤、回合数。
- **通过标准**：`atr_dual` 净 PnL 显著优于 `fixed`，且回合数明显下降（手续费随之降）。不达标不切实盘。

## 5. 测试

单元测试覆盖：

- ATR 计算正确性（含数据不足 / 全平 K 线边界）
- 软/硬距离公式（floor 与 cap 边界）
- 等风险仓位公式（$2,000 上限、最小下单额放弃路径）
- 软止损收盘确认触发判断（LONG / SHORT 双向）
- 存量仓位兼容回退（无新字段时的行为）

现有 `tests/` 全量回归。

## 6. 风险与已知取舍

- 软止损仅收盘确认 → 单根 K 线内最大亏损可达硬止损（约 2 倍软止损距离）。这是"扛噪音"的代价，靠等风险缩仓使美元风险不变。
- 机器人离线时只有硬止损保护（交易所挂单），软止损暂停——与现状的离线保护等级一致。
- 高波动币缩仓后名义变小，手续费占比略升；但预期回合数下降带来的降费远大于此。
- CLAUDE.md 中"ATR-based dynamic trailing stop"的描述早已过时（实际为固定 2% + 移动止盈），本次实现后应顺手更新 CLAUDE.md 的风控描述。
