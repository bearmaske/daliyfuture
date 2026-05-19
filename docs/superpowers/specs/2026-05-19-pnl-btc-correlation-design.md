# 实盘亏损周期 vs BTC 指标相关性分析

**日期**：2026-05-19
**作者**：danny + Claude
**状态**：设计批准，待实现

## 问题陈述

用户观察到 Testnet 实盘账户（2026-05-01 ~ 05-18）收益呈周期性——大盘大幅涨/跌时有收益，其余时段持续亏损。期望通过定量分析找出 **哪些 BTC 市场指标** 与亏损时段相关，以便后续策略迭代时增加过滤条件。

## 数据来源

| 数据 | 文件 / 来源 | 时间范围 |
|---|---|---|
| 账户逐笔损益 | `results/live_income_20260501_20260518.csv` | 2026-05-01 ~ 05-18 |
| BTC 行情 | Binance mainnet `klines` (1H) | 2026-04-24 ~ 05-19（前置 7 天 warmup） |

`live_income.csv` 列：`time, symbol, incomeType, income, asset, tranId, info`，其中 `incomeType ∈ {REALIZED_PNL, COMMISSION, FUNDING_FEE}`，全部累加得到净 P&L。

## 时间粒度

- **小时**：作主分析口径，~432 数据点，足以做相关性检验
- **日**：仅用于可视化叠加，不参与统计

## BTC 指标矩阵

| 类别 | 字段名 | 计算方式 |
|---|---|---|
| 波动率 | `ret_std_24h` | 1H log return 在过去 24H 的 std |
| 波动率 | `atr_14` | 14H ATR / 收盘价 (归一化) |
| 成交量 | `vol_ratio_20` | volume / SMA20(volume) |
| 成交量 | `vol_zscore_50` | log(volume) 50H z-score |
| 趋势 | `sma20_slope` | SMA20 当前 vs 5H 前的相对斜率 |
| 趋势 | `sma20_50_dist` | (SMA20 − SMA50) / SMA50 |
| 动量 | `roc_6`, `roc_12`, `roc_24` | close 相对 N 小时前的变化率 |
| 布林 | `bb_width` | (上轨 − 下轨) / 中轨，20H BB |
| 布林 | `bb_pctb` | (close − 下轨) / (上轨 − 下轨) |
| 价格行为 | `hl_range` | (high − low) / close |

所有指标可能 NaN 的初始窗口由 warmup 数据填充，分析阶段只取 5-01 ~ 5-18 范围。

## P&L 序列构造

```
pnl_hourly[t] = sum(income for income in live_income where floor(time, '1H') == t)
```

时间戳按本地时区（UTC+8）落桶，对齐到 BTC K线 `open_time` 同一时区。空小时填 0。

## 统计输出

1. **相关性**：对每个 BTC 指标 vs `pnl_hourly`，计算 Pearson 与 Spearman 相关系数 + p 值。
2. **亏损 vs 盈利窗口对比**：把小时按 `pnl > 0 / pnl < 0 / pnl == 0` 分组，对每个 BTC 指标做：
   - 均值 / 中位数 / std
   - Mann–Whitney U 检验（亏损 vs 盈利分布差异）
3. **Top 影响因子**：按 |Spearman| × (1 − p) 降序排，输出前 5 给出业务解读。

## 交付物

- `scripts/analyze_pnl_vs_btc.py` — 一次性分析脚本，可用 CLI 参数覆盖输入路径
- `results/pnl_btc_correlation_report.md` — markdown 报告，含相关性表 + 分组对比表 + Top 因子解读
- `results/pnl_btc_chart.png` — 双轴折线：日累计净 P&L vs BTC 波动率（`ret_std_24h`）与 BB 带宽

## 范围之外（YAGNI）

- 不做多因子回归 / 任何机器学习预测（n=432 容易过拟合）
- 不动 `strategy.py` / `risk.py` / 实盘代码
- 不引入新依赖（用现有 pandas + numpy + scipy + matplotlib）

## 风险与已知限制

- 仅 18 天样本，统计显著性有限——相关性结论只能作为「下一步实验假设」，不能直接作为策略过滤规则
- Testnet 数据与 mainnet 行情时间一致，但成交滑点 / 资金费率可能与主网不完全对等，分析结论应聚焦在 BTC 指标的 *相对差异*，而不是绝对 P&L 数值
- BTC 数据下载依赖外网，失败时脚本应给出明确错误（fail loud）

## 自审检查（内联）

- [x] 无 TBD / 占位符
- [x] 章节互相一致（数据范围、粒度、指标矩阵、交付物互相对齐）
- [x] 范围适合单次实现 plan（无需拆分子项目）
- [x] 关键术语唯一定义（"净 P&L" = REALIZED_PNL+COMMISSION+FUNDING_FEE 求和；"亏损窗口" = pnl_hourly<0）
