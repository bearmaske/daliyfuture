# Trend Sniper — Binance 加密货币合约交易 Bot

基于双时间框架布林带的加密货币 USDT 永续合约动量策略。支持模拟盘（Testnet）和实盘（Mainnet）两种模式，通过环境变量一键切换。

## 策略简介

### 入场逻辑

三层过滤 + 优先排序：

1. **选币**：24h 成交额 Top-50 池（默认），排除稳定币对和股票/预上市类永续（TSLA/COIN/MSTR 等）。候选需满足 24h 成交额 ≥ `MIN_QUOTE_VOLUME_24H`（默认 $50M）。

2. **日线趋势过滤**（`TREND_FILTER_MODE = "bb_middle"`）：
   - 日线收盘价 > 日线布林中轨 → 只做多
   - 日线收盘价 < 日线布林中轨 → 只做空

3. **1H 布林带突破 + 24H 极值确认 + 6H 中轨同侧**（三条件同时满足）：
   - 做多：1H 收盘价突破 1H 布林上轨，**且**该 1H 最高价是过去 24 根 1H 的最高价，**且** 1H 收盘价位于 6H 布林中轨上方
   - 做空：1H 收盘价跌破 1H 布林下轨，**且**该 1H 最低价是过去 24 根 1H 的最低价，**且** 1H 收盘价位于 6H 布林中轨下方
   - 6H 中轨过滤由 `H6_MIDDLE_FILTER_ENABLED` 开关控制（默认开启）
   - 信号在 1H 收盘确认，下一根 1M 开盘价（市价单）入场

4. **交易量优先**：多信号并发时，按 24h 成交量从大到小排序开仓，优先流动性好的币种。

### 出场逻辑

每笔持仓同时设置两道出场线，任意触发即平仓：

| 类型 | 参数 | 说明 |
|------|------|------|
| 固定止损 | `FIXED_STOP_LOSS_PCT = 2%` | 开仓后立即在 Binance 挂 `STOP_MARKET` 订单，由交易所直接执行 |
| 激活式移动止盈 | `TRAILING_ACTIVATION_PCT = 3%` | 浮盈达到 3% 后激活，本地轮询触发 |
| 移动止盈回撤 | `TRAILING_DRAWDOWN_PCT = 1.5%` | 激活后，从最有利价格回撤 1.5% 出场 |

**固定止损**：开仓后立即通过 `POST /fapi/v1/algoOrder`（`algoType=CONDITIONAL`）在 Binance 挂 `STOP_MARKET` 条件单（止损价 = 入场价 ± 2%）。止损由交易所直接执行，不依赖 bot 在线。每 60 秒查询订单状态：
- `FILLED` → 本地记录平仓，撤销移动止盈单（OCO）
- `CANCELED/EXPIRED` → 自动重新挂单
- 查询失败 → 本地价格检查兜底

**移动止盈**：两层机制配合，消除轮询盲区：

1. **WebSocket 实时激活**（`watcher.py`）：后台订阅 `<symbol>@markPrice` 流，每秒收到标记价格。浮盈达到 3% 时立刻以**当时最高价**为激活价，通过 `POST /fapi/v1/algoOrder` 挂 `TRAILING_STOP_MARKET`（回调率 1.5%，`workingType=MARK_PRICE`）。挂单成功后自动取消订阅。
2. **交易所实时追踪**：`TRAILING_STOP_MARKET` 挂上后，交易所实时跟踪最高/最低价，回撤 1.5% 自动触发，不依赖 bot 轮询。
3. **轮询兜底**（每 60 秒）：查询移动止盈单状态，`FILLED` 撤销固定止损单并记录平仓；`CANCELED/EXPIRED` 用当前极值价重新挂单；WebSocket 挂单失败时本地逻辑托底。

两个交易所订单形成手动 OCO：任意一个触发，自动撤销另一个。

> **接口说明**：Binance 已将 `STOP_MARKET` / `TRAILING_STOP_MARKET` 迁移至 Algo Order API（`/fapi/v1/algoOrder`），不再使用 `/fapi/v1/order`。主网和测试网均已支持。

### 全局风控

#### 同币种冷却（Per-Symbol Cooldown）

- 近 24h 内同币亏损 ≥ 2 笔 → 进入 24h 冷却期，期间跳过该币的扫描和开仓
- 避免同一币反复突破反复止损、越亏越深

#### Binance 风控黑名单

- 下单时 Binance 返回 `-4106`/`-4129`/`-4131`（position risk control）→ 该币立即加入 24h 黑名单
- 黑名单持久化到 `state.json`，重启恢复，过期自动清理

#### 全局熔断（Circuit Breaker）

- 总资产回撤超过 `MAX_DRAWDOWN_PCT`（默认 20%）→ 强制平仓所有持仓，进入 24h 冷静期
- 冷静期内暂停策略扫描，止损检查正常运行

#### 日内亏损熔断（Daily Drawdown Guard）

- 每小时策略扫描前，将当前总资产与当日 00:00 快照对比
- 日内跌幅 ≥ 20% → 立即进入 `COOLDOWN_HOURS`（默认 24h）冷静期，暂停开仓
- 每天凌晨 00:00 自动更新快照，熔断到期后自动恢复

#### 永久黑名单（Symbol Blacklist）

- `SYMBOL_BLACKLIST`：扫描时直接跳过，不受成交量、冷却期等条件影响
- 默认包含无法交易的 TradFi 衍生品（未签署协议）：`XAUUSDT`（黄金）、`XAGUSDT`（白银）、`BZUSDT`（布伦特原油）

### 数据处理

- **行情数据**始终从 Binance 主网获取（数据质量更好）
- **下单执行**根据 `TRADING_MODE` 走 Testnet 或主网
- K 线计算时丢弃最后一根未闭合的蜡烛，避免用不完整数据做决策
- 成交价以 Binance 返回的 `avgPrice`/`executedQty` 为准，PnL 用 `qty × (exit − entry)` 计算，与交易所账户一致

### 手续费

- 平仓后按 orderId 调 `GET /fapi/v1/userTrades`，拿每笔成交的 `commission` 字段（开+平双边净额）
- 心跳同时查询 `GET /fapi/v1/income?incomeType=COMMISSION`，对比两个来源
- 自动处理 USDT 和 BNB 抵扣两种手续费类型

---

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# 交易模式: "paper"(模拟盘) 或 "live"(实盘)
TRADING_MODE=paper

# Binance Testnet API（模拟盘）申请地址：https://testnet.binancefuture.com
BINANCE_TESTNET_API_KEY=你的key
BINANCE_TESTNET_API_SECRET=你的secret

# Binance 主网 API（实盘，只开合约交易权限，不开提币权限）
BINANCE_LIVE_API_KEY=你的key
BINANCE_LIVE_API_SECRET=你的secret

# 通知渠道（默认全关，按需开启）
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

PUSHDEER_ENABLED=false
PUSHDEER_KEYS=

BARK_ENABLED=false
BARK_URLS=
```

> 模拟盘建议开 PushDeer 或 Telegram；实盘建议开 Bark。把对应 `_ENABLED` 改为 `true` 并填入密钥即可。

### 3. 启动 Bot

```bash
source .venv/bin/activate
python main.py
```

Bot 启动后持续运行：
- 启动时**不立即开仓**，等待到下一个整点 `:01` 才执行首次扫描，启动日志和通知中会显示首次扫描时间及倒计时
- 启动时立即检查已有持仓的止损/止盈状态
- 每小时 :01 扫描市场前先做**日内跌幅检查**，跌幅 ≥ 20% 自动触发冷静期
- 每 60 秒检查持仓止损/止盈订单状态
- WebSocket 后台线程实时监控标记价格，触发移动止盈激活
- 每 6 小时推送策略执行汇报（含当前单仓金额、今日快照、日内盈亏变动）
- 每天 00:00 记录余额快照并动态调整单仓金额（总资产 × 5%，向下取整到十位）

### 4. 停止 Bot

按 `Ctrl+C`，或后台运行时：

```bash
pkill -f "python main.py"
```

---

## 运行测试

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

---

## 项目结构

```
daliyfuture/
├── main.py              # 入口，APScheduler 调度 + WebSocket 生命周期管理
├── config.py            # 所有策略参数 + 环境变量加载
├── exchange.py          # Binance API 封装（行情 + 下单 + 止损/移动止盈单）
├── strategy.py          # 入场策略（日线趋势 + 1H 布林带 + 24H 极值）
├── risk.py              # 止损止盈（固定止损 + 移动止盈 + 全局熔断）
├── watcher.py           # WebSocket 实时标记价格监控，触发移动止盈激活
├── notifier.py          # 日志 + 通知路由
├── state.py             # JSON 状态持久化（持仓、交易历史、黑名单）
├── tests/               # 单元测试
├── backtesting/         # 独立回测模块
│   ├── engine.py        # 回测引擎（复用 strategy/risk 核心逻辑）
│   ├── backtest.py      # 回测 CLI 入口
│   ├── download_data.py # K 线数据下载工具
│   └── report.py        # 统计报告（Sharpe、最大回撤、胜率）
├── .env.example         # 环境变量模板
├── requirements.txt     # Python 依赖
└── state.json           # 运行时状态（自动生成）
```

---

## 策略参数

所有参数在 `config.py` 的 `Config` dataclass 中定义，直接修改即可。

### 资金 & 仓位

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `INITIAL_CAPITAL` | 8084 | 初始资金（USDT） |
| `POSITION_SIZE` | 400 | 单仓保证金（USDT），每日 00:00 动态调整为总资产 × 5%（向下取整到十位） |
| `MAX_POSITIONS` | 10 | 最大同时持仓数 |
| `LEVERAGE` | 5 | 杠杆倍数 |

### 止损止盈

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `FIXED_STOP_LOSS_PCT` | 0.02 | 固定止损 2% |
| `TRAILING_ACTIVATION_PCT` | 0.03 | 移动止盈激活阈值（浮盈≥3%） |
| `TRAILING_DRAWDOWN_PCT` | 0.015 | 移动止盈激活后回撤触发线（1.5%） |

### 全局风控

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MAX_DRAWDOWN_PCT` | 0.20 | 全局熔断：总资产回撤 20% 触发强平 |
| `COOLDOWN_HOURS` | 24 | 熔断后冷静期（小时） |
| `SYMBOL_LOSS_THRESHOLD` | 2 | 同币种冷却触发阈值（近窗口内亏损笔数） |
| `SYMBOL_COOLDOWN_WINDOW_HOURS` | 24 | 同币种冷却观察窗口（小时） |
| `SYMBOL_COOLDOWN_HOURS` | 24 | 同币种冷却时长（小时） |
| `POSITION_RISK_BLACKLIST_HOURS` | 24 | Binance 风控拒单后黑名单时长（小时） |

### 策略信号

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TREND_FILTER_MODE` | "bb_middle" | 日线趋势过滤：`bb_middle` / `sma` / `rolling_sma` / `asymmetric` / `disabled` |
| `SMA_PERIOD` | 20 | 日线趋势 SMA 周期 |
| `BB_PERIOD` | 20 | 布林带周期 |
| `BB_STD` | 2.0 | 布林带标准差倍数 |
| `H6_MIDDLE_FILTER_ENABLED` | True | 6H 布林中轨同侧过滤开关（叠加在日线趋势 + 1H 突破 + 24H 极值之上） |

### 选币

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TOP_SYMBOLS_COUNT` | 50 | 扫描 24h 成交量前 N 币种 |
| `MIN_QUOTE_VOLUME_24H` | 50_000_000 | 24h 成交额下限（USDT） |
| `EXCLUDE_EQUITY_PERPS` | True | 跳过股票/预上市类永续 |
| `EXCLUDE_TOP10_SYMBOLS` | `[]` | 需排除的主流币列表（默认不排除） |
| `SYMBOL_BLACKLIST` | 见下方 | 永久黑名单，扫描时直接跳过 |

**默认黑名单**（`SYMBOL_BLACKLIST`）：

| 交易对 | 原因 |
|--------|------|
| `MEGAUSDT` | 手动加入 |
| `XAUUSDT` | TradFi 衍生品协议未签署（-4411） |
| `XAGUSDT` | TradFi 衍生品协议未签署（-4411） |
| `BZUSDT` | TradFi 衍生品协议未签署（-4411） |

---

## 通知

| 模式 | 通知渠道 | 标题前缀 |
|------|----------|----------|
| 模拟盘 (`paper`) | PushDeer + Telegram | `[模拟]` |
| 实盘 (`live`) | Bark | `[实盘]` |

每 6 小时自动推送心跳汇报，包含：总资产、当前单仓金额、今日 00:00 快照与日内盈亏变动、持仓 PnL、交易统计（胜率、已实现 PnL、手续费）。

---

## 回测

```bash
# 下载历史 K 线（约 1 年，30 个主流币）
python backtesting/download_data.py

# 全量回测
python -m backtesting.backtest

# 自定义参数
python -m backtesting.backtest --symbols BTCUSDT,ETHUSDT --capital 20000 --leverage 10
```

回测引擎复用与实盘相同的入场/出场逻辑，包含 taker 手续费（0.04%）和滑点（0.05%）模拟。

---

## 注意事项

- **默认模拟盘**，不涉及真实资金。切换实盘前请充分了解策略风险
- 支持单向持仓（One-way）和双向持仓（Hedge Mode）
- 实盘 API Key 建议：**只开合约交易权限，不开提币权限，绑定 IP 白名单**
- Testnet API Key 需在 [testnet.binancefuture.com](https://testnet.binancefuture.com) 单独申请
- 建议用 `tmux` 或 `screen` 在后台长期运行
