# Trend Sniper — Binance 加密货币合约交易 Bot

基于双时间框架布林带的加密货币 USDT 永续合约动量策略。支持模拟盘（Testnet）和实盘（Mainnet）两种模式，通过环境变量一键切换。

## 策略简介

### 入场逻辑

三层过滤 + 优先排序：

1. **选币**：24h 成交额 Top-50 池（默认），排除稳定币对和股票/预上市类永续（TSLA/COIN/MSTR 等，`underlyingType=EQUITY/PREMARKET`）。候选需满足 24h 成交额 ≥ `MIN_QUOTE_VOLUME_24H`（默认 $50M）。`EXCLUDE_TOP10_SYMBOLS` 默认为空列表（主流币 BTC/ETH 等纳入扫描），需要剔除时自行填入
   - 可选的 **1H 爆量池**（`ENABLE_1H_SPIKE_POOL`，默认 **关闭**）：追加最近一根已闭合 1H 成交额 ≥ `MIN_1H_QUOTE_VOLUME`（默认 $10M）的币，用于捕捉"爆涨暴跌"刚发生但 24h 均量还没跟上的情形
2. **日线趋势判断**（可配置模式，`TREND_FILTER_MODE`）：
   - `sma`（默认）：SMA 斜率方向 + 价格位置 → 判断做多/做空/跳过
   - `bb_middle`：价格 > 日线布林中轨 → 只做多，< 中轨 → 只做空
   - `disabled`：不做日线过滤，仅靠小时线突破方向决定多空
3. **波动率过滤**（`VOL_FILTER_ENABLED`）：比较短期 ATR(7) 与长期 ATR(28) 的比值，低于阈值说明波动率收缩（震荡市），跳过开仓
4. **小时线布林带突破确认**：
   - 做多方向：1H 收盘价突破上轨 → 开多
   - 做空方向：1H 收盘价跌破下轨 → 开空
5. **交易量优先开仓**：当同时出现多个信号时，先收集所有信号，按 24h 交易量从大到小排序，优先开仓交易量大的币种
6. **记账对齐交易所**：市价单成交后读取 Binance 返回的 `avgPrice`/`executedQty`，以此记录入场/出场价和实际成交量。PnL 用 `qty × (exit − entry)` 计算，和 Binance 账户一致（早期版本用下单前 ticker 价 + 名义公式，会出现几十美金量级的偏差）

核心思路：日线确认趋势方向，波动率过滤排除低波动震荡市假突破，小时线捕捉突破入场点。高交易量优先确保流动性充足、滑点更低。

### 出场逻辑

两层风控：**ATR 动态移动止损** + **全局熔断**

#### 单仓止损（ATR 移动止损）

- 用 14 周期 ATR（1H K线）衡量当前波动率
- 多单止损线 = 历史最高价 - 2.0 × ATR，只升不降
- 空单止损线 = 历史最低价 + 2.0 × ATR，只降不升
- 兜底：无论 ATR 算出多少，最大回撤不超过 6%

波动大的币自动放宽止损（不被正常波动洗掉），波动小的币自动收紧（及时止损）。没有固定止盈，让利润奔跑。

#### 全局熔断（Circuit Breaker）

- 每次止损检查时，先检测总资产是否跌破初始资金的 85%（即亏损 15%）
- 触发后**强制平仓所有持仓**，进入 **24 小时冷静期**
- 冷静期内策略暂停扫描和开仓，止损检查继续运行
- 冷静期到期后自动恢复正常交易
- 通过 `MAX_DRAWDOWN_PCT` 和 `COOLDOWN_HOURS` 配置

### 手续费

- **每笔重建**：平仓后心跳任务按 orderId 调 `GET /fapi/v1/userTrades`，拿到每笔成交的 `commission` 字段（开仓+平仓双边），扣除后得到单笔净 PnL
- **平台权威值**：心跳同时调 `GET /fapi/v1/income?incomeType=COMMISSION`（分页聚合全部记录），得到 Binance 实际收取的累计手续费
- 汇报里两个数值**并列显示**（"累计手续费(重建)" vs "平台手续费(/income)"），两者出现差距即说明有 trade 的 orderId 丢失或成交记录未返回
- 自动处理 USDT 和 BNB 抵扣两种手续费类型

### 资金费率

- 开仓通知中显示当前资金费率和下次收取时间
- 自动判断当前仓位方向是付出还是收取资金费率（正费率：多头付空头；负费率：空头付多头）

### 数据处理

- **行情数据**始终从 Binance 主网获取（数据质量更好）
- **下单执行**根据 `TRADING_MODE` 走 Testnet 或主网
- K 线计算时丢弃最后一根未闭合的蜡烛，避免用不完整数据做决策
- 启动时和每次策略扫描前，自动同步账户数据（余额 + 持仓）

## 快速开始

### 1. 安装依赖

```bash
cd dabao
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的密钥：

```bash
# 交易模式: "paper"(模拟盘) 或 "live"(实盘)
TRADING_MODE=paper

# Binance Testnet API（模拟盘）
# 申请地址：https://testnet.binancefuture.com
BINANCE_TESTNET_API_KEY=你的key
BINANCE_TESTNET_API_SECRET=你的secret

# Binance 主网 API（实盘 — 只开合约交易权限，不要开提币权限）
BINANCE_LIVE_API_KEY=你的key
BINANCE_LIVE_API_SECRET=你的secret

# --- 通知渠道（实盘用 Bark，模拟盘用 PushDeer + Telegram）---

# Telegram（模拟盘通道）
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=你的bot_token
TELEGRAM_CHAT_ID=你的chat_id

# PushDeer（模拟盘通道，多个 key 逗号分隔）
PUSHDEER_ENABLED=false
PUSHDEER_KEYS=你的pushkey1,你的pushkey2

# Bark（实盘通道，多个 URL 逗号分隔）
BARK_ENABLED=false
BARK_URLS=https://api.day.app/key1,https://api.day.app/key2
```

> **默认模拟盘模式。** 切换实盘：将 `TRADING_MODE` 改为 `live` 并填入主网 API 密钥。
> **通知渠道默认关闭。** 模拟盘开启 PushDeer/Telegram，实盘开启 Bark，把对应的 `_ENABLED` 改为 `true` 并填入密钥。

### 3. 启动 Bot

```bash
source .venv/bin/activate
python main.py
```

Bot 启动后会持续运行：
- 启动时立即执行一次策略扫描和止损检查
- 每小时 :01 扫描市场并检查入场信号
- 每 60 秒检查持仓 ATR 移动止损
- 每 6 小时发送策略执行汇报（账户状态、持仓盈亏、胜率）

### 4. 停止 Bot

按 `Ctrl+C`，Bot 会保存状态后优雅退出。

## 运行测试

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## 项目结构

```
dabao/
├── main.py          # 入口，APScheduler 调度 + 策略执行汇报
├── config.py        # 参数配置 + 环境变量加载
├── exchange.py      # Binance API 封装（主网行情 + 模拟/实盘下单 + 手续费提取）
├── strategy.py      # 布林带策略（SMA 斜率趋势判断 + 突破入场信号）
├── risk.py          # ATR 动态移动止损 + 全局熔断强平
├── notifier.py      # 日志 + 通知路由（实盘→Bark / 模拟→PushDeer+Telegram）
├── state.py         # JSON 状态持久化 + 账户同步
├── tests/           # 单元测试
├── .env.example     # 环境变量模板
├── requirements.txt # Python 依赖
└── state.json       # 运行时状态（自动生成）
```

## 策略参数

所有参数在 `config.py` 中定义，可直接修改：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TRADING_MODE` | "paper" | 交易模式：`paper`（模拟盘）/ `live`（实盘） |
| `INITIAL_CAPITAL` | 10000 | 初始资金（USDT） |
| `POSITION_SIZE` | 500 | 单仓保证金（USDT） |
| `MAX_POSITIONS` | 10 | 最大同时持仓数 |
| `LEVERAGE` | 5 | 杠杆倍数 |
| `TREND_FILTER_MODE` | "sma" | 日线趋势过滤模式：`sma`（默认） / `rolling_sma`（1H 滚动，更敏感） / `asymmetric`（LONG 用 daily-sma、SHORT 用 rolling-sma） / `bb_middle` / `disabled`。一年回测下 `sma` 最优 |
| `SMA_PERIOD` | 20 | 日线趋势 SMA 周期（独立于布林带周期） |
| `VOL_FILTER_ENABLED` | True | 波动率过滤开关 |
| `VOL_ATR_SHORT` | 7 | 短期 ATR 窗口（近期波动率） |
| `VOL_ATR_LONG` | 28 | 长期 ATR 窗口（基线波动率） |
| `VOL_ATR_THRESHOLD` | 1.2 | 短/长 ATR 比值阈值，低于此值跳过开仓 |
| `ATR_PERIOD` | 14 | 止损 ATR 计算周期 |
| `ATR_MULTIPLIER` | 2.0 | ATR 止损倍数 |
| `MAX_STOP_LOSS` | 0.06 | 最大止损百分比（6% 兜底） |
| `MAX_DRAWDOWN_PCT` | 0.15 | 全局熔断阈值（总资产回撤 15% 触发强平） |
| `COOLDOWN_HOURS` | 24 | 熔断后冷静期时长（小时） |
| `BB_PERIOD` | 20 | 布林带周期 |
| `BB_STD` | 2.0 | 布林带标准差倍数 |
| `TOP_SYMBOLS_COUNT` | 50 | 扫描成交量前 N 币种 |
| `EXCLUDE_EQUITY_PERPS` | True | 跳过股票/预上市类永续（EQUITY / PREMARKET） |
| `EXCLUDE_TOP10_SYMBOLS` | `[]`（空） | 需要剔除的主流币列表，默认不剔除 |
| `MIN_QUOTE_VOLUME_24H` | 50_000_000 | 24h 成交额下限（USDT），低于此值不进入 24h 池 |
| `ENABLE_1H_SPIKE_POOL` | False | 是否启用 1H 爆量追加池 |
| `MIN_1H_QUOTE_VOLUME` | 10_000_000 | 1H 爆量池下限（USDT），最近一根闭合 1H 成交额达到即纳入 |
| `RISK_CHECK_INTERVAL_SECONDS` | 60 | 止损检查间隔（秒）。回测显示 60s 在 PnL 和 DD 之间平衡最好 |
| `STRATEGY_START_TIME` | "2026-04-13 00:00:00" | 策略起始时间（UTC+8），用于心跳汇报显示运行时长 |

## 通知

模拟盘和实盘使用**不同的通知渠道**，互不干扰：

| 模式 | 通知渠道 | 标题前缀 |
|------|----------|----------|
| 模拟盘 (`paper`) | PushDeer + Telegram | `[模拟]` |
| 实盘 (`live`) | Bark | `[实盘]` |

## 策略执行汇报

每 6 小时自动推送一次汇报，内容包含：

```
--- 资产概览 ---
总资产: $10538.26 | 盈利: +$538.26 (+5.38%)
钱包余额: $10400.00 | 可用余额: $9400.00
持仓未实现PnL: +$138.26
初始资金: $10000.00
--- 交易统计 (实盘) ---
运行时长: 7天12小时30分钟 (自 2026-04-09 08:00:00)
持仓: 2/10
已平仓: 3 笔 | 胜率: 67% (2胜/1负)
累计已实现PnL: +$400.00 | 累计手续费(重建): $2.4000
平台手续费(/income): $2.4000
--- 当前持仓 ---
DOTUSDT SHORT | 入场: 1.3890 | 现价: 1.3740 | +$54.00 (+10.8%)
BTCUSDT LONG | 入场: 70000.0000 | 现价: 70500.0000 | +$17.86 (+3.6%)
--- 策略状态 ---
策略运行正常
```

## 日志

- 终端实时输出 + 写入 `logs/dabao_YYYY-MM-DD.log`（按日滚动，UTC+8）
- 启动时打印完整配置摘要、通知渠道状态、调度信息、运行时长
- **扫描启动**：全网/候选/24h 池/1H 爆量池的数量，成交额区间，1H 爆量池每个币种带成交额
- **每币扫描**：趋势、日线中轨、BB 上/中/下轨、带宽 %、当前价在带内位置 %、波动率比值、24h 成交量、是否出信号；无趋势时额外显示 SMA 斜率方向
- **扫描结束**：跳过原因分类汇总（已持仓 / 日线不足 / 无趋势 / 波动率收缩 / 无突破 / 异常）
- **开仓日志**：符号、方向、入场价、数量、名义金额、保证金、杠杆、orderId、余额
- **止损检查**：持仓时长（`3h12m`）、未实现 PnL（美金+百分比）、ATR 值、止损线、当前生效的止损类型（ATR / 6% 兜底）、距止损线距离、回撤/反弹幅度
- **平仓日志**：持仓时长、入场 → 出场价、实现 PnL + 百分比、orderId、余额

## 回测

`backtesting/` 下的独立 CLI，用历史数据重放同一套策略。

```bash
# 下载历史 K 线到 data/（支持断点续下 + 向前/向后回填）
python backtesting/download_data.py --days 365 --intervals 1h,1d
python backtesting/download_data.py --days 365 --intervals 1m   # 可选，仅对比扫描频率时需要

# 一年回测（1H 分辨率）
python -m backtesting.backtest
python -m backtesting.backtest --symbols BTCUSDT,ETHUSDT --capital 20000 --leverage 10

# 对比不同止损扫描频率（需要 1m 数据）
python -m backtesting.compare_stop_cadence

# 对比不同趋势过滤模式（sma / rolling_sma / asymmetric）
python -m backtesting.compare_trend_mode
```

一年回测参考（2025-04 → 2026-04, 29 币）：

| cadence | PnL | 年化 | Max DD | Sharpe | PF |
|---|---|---|---|---|---|
| 1m | +$20,738 | +207% | 44.3% | 1.13 | 1.40 |
| 2m | +$19,634 | +196% | 16.4% | **1.19** | 1.39 |
| 3m | +$17,178 | +172% | 19.1% | 1.19 | 1.34 |

扫描越密 → PnL 略高、"噪声止损"增多、回撤可能显著放大。当前默认值 60 秒是 PnL 偏优档；若更看重资金曲线平滑度，可改为 120 秒。

## 注意事项

- **默认模拟盘**，不涉及真实资金。切换实盘请确认已充分了解策略风险
- 自动检测账户持仓模式，**单向持仓（One-way）和双向持仓（Hedge Mode）均兼容**
- 实盘 API Key 建议：**只开合约交易权限，不开提币权限，绑定 IP 白名单**
- Binance Testnet API Key 需要在 [testnet.binancefuture.com](https://testnet.binancefuture.com) 单独申请
- Testnet 的撮合引擎和主网独立，订单簿可能较薄
- 建议用 `tmux` 在后台运行 Bot
