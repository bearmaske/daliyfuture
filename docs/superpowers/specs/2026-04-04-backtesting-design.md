# Backtesting System Design

## Overview

为 Trend Sniper 交易机器人添加历史回测能力，使用最近1年的Binance K线数据，中等仿真度（手续费、滑点、资金管理），验证现有策略的历史表现。

## Architecture

新增4个文件，不修改现有代码：

```
backtesting/
├── download_data.py    # 数据下载脚本
├── backtest.py         # 回测主入口（CLI）
├── engine.py           # 回测核心引擎
└── report.py           # 结果统计与输出
data/                   # K线数据存放（gitignore）
results/                # 回测结果输出（gitignore）
```

## Module Design

### 1. download_data.py — 数据下载

**职责**: 从Binance mainnet拉取历史K线并存为本地CSV。

- 复用 `exchange.py` 的 `data_client`（mainnet，无需auth）
- 拉取每个symbol的 1H 和 1D K线，时间范围：最近1年
- 存储路径: `data/{SYMBOL}_{interval}.csv`（如 `data/BTCUSDT_1h.csv`）
- CSV列: `open_time, open, high, low, close, volume`
- 断点续传: 检查已有CSV的最后时间戳，只拉取增量数据
- Binance API 每次最多1500根K线，需分批拉取
- 用法: `python backtesting/download_data.py`

### 2. engine.py — 回测引擎

**职责**: 逐bar推进时间线，模拟策略信号判断和交易撮合。

**时间推进**:
- 以1H bar为最小步进单位
- 每个bar: 先检查持仓止损，再检查新入场信号

**信号判断**:
- 直接import并调用 `strategy.py` 中的信号函数（`check_trend`, Bollinger Bands计算）
- 传入截止当前bar的历史K线切片（不含当前未闭合bar）
- 保证回测逻辑与实盘完全一致

**止损检查**:
- 复用 `risk.py` 的ATR计算逻辑
- 每个bar更新持仓的 highest/lowest price
- 检查ATR trailing stop 和 hard cap stop

**撮合模拟**:
- 信号产生在当前bar close，成交价为下一bar的open price
- 避免未来数据偷看（look-ahead bias）

**费用模型**:
- 手续费: taker 0.04% 开仓 + 0.04% 平仓（共0.08%）
- 滑点: 固定0.05%（对long开仓价上浮，short开仓价下浮）

**资金管理**:
- 维护虚拟余额，初始值 = `Config.INITIAL_CAPITAL`
- 每笔开仓扣除 `Config.POSITION_SIZE`，平仓归还本金+PnL-手续费
- 余额不足 POSITION_SIZE 时跳过开仓
- 最大同时持仓数 = `Config.MAX_POSITIONS`

**数据结构**:
```python
@dataclass
class BacktestPosition:
    symbol: str
    side: str           # "LONG" / "SHORT"
    entry_price: float
    quantity: float
    highest_price: float
    lowest_price: float
    opened_at: str      # ISO timestamp

@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float          # 扣除手续费后的净PnL
    fee: float
    opened_at: str
    closed_at: str
    exit_reason: str     # "atr_stop" / "hard_stop"
```

### 3. report.py — 结果输出

**终端打印指标**:
- 总收益 / 总收益率
- 年化收益率
- 最大回撤（金额和百分比）
- 夏普比率（年化，无风险利率=0）
- 胜率（盈利交易数 / 总交易数）
- 盈亏比（平均盈利 / 平均亏损）
- 总交易次数
- 平均持仓时间
- 多空交易占比

**CSV导出**:
- `results/trades.csv`: 每笔交易明细（BacktestTrade字段）
- `results/equity.csv`: 逐小时权益曲线（timestamp, equity, drawdown）

### 4. backtest.py — 主入口

**命令行参数**（全部可选，有默认值）:
- `--symbols`: 逗号分隔的symbol列表（默认15个主流币）
- `--start`: 起始日期（默认1年前）
- `--end`: 结束日期（默认今天）
- `--capital`: 初始资金（默认Config.INITIAL_CAPITAL）
- `--position-size`: 每笔仓位大小（默认Config.POSITION_SIZE）
- `--leverage`: 杠杆倍数（默认Config.LEVERAGE）

**执行流程**:
1. 解析参数
2. 从 `data/` 加载CSV数据
3. 初始化引擎
4. 逐bar运行
5. 生成报告并输出

**用法**: `python backtesting/backtest.py [--symbols BTCUSDT,ETHUSDT] [--capital 10000]`

## Default Symbol List

15个主流USDT永续合约:
BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, AVAXUSDT, LINKUSDT, DOTUSDT, MATICUSDT, UNIUSDT, LTCUSDT, ATOMUSDT, NEARUSDT

## Key Design Decisions

1. **复用不修改**: import现有 `strategy.py` 和 `risk.py` 的函数，保证回测与实盘逻辑一致
2. **下一bar open成交**: 避免look-ahead bias
3. **数据与回测分离**: 下载和回测是两个独立步骤，回测可反复运行不用重新下载
4. **Config复用**: 默认参数与实盘一致，CLI参数可覆盖

## Dependencies

现有依赖即可满足（pandas用于CSV处理，python-binance用于数据下载）。无需新增第三方库。
