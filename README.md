# Trend Sniper — Binance 加密货币合约交易 Bot

基于双时间框架布林带的加密货币 USDT 永续合约动量策略。支持模拟盘（Testnet）和实盘（Mainnet）两种模式，通过环境变量一键切换。

## 策略简介

### 入场逻辑

多层过滤 + 优先排序：

1. **选币**：24h 成交额 Top-50 池（默认），排除稳定币对和股票/预上市类永续（TSLA/COIN/MSTR 等）。候选需满足 24h 成交额 ≥ `MIN_QUOTE_VOLUME_24H`（默认 $50M）。

2. **日线趋势过滤**（`TREND_FILTER_MODE = "bb_middle"`）：
   - 日线收盘价 > 日线布林中轨 → 只做多
   - 日线收盘价 < 日线布林中轨 → 只做空

3. **1H 布林带突破 + 24H 极值确认 + 6H 中轨同侧**（三条件同时满足）：
   - 做多：1H 收盘价突破 1H 布林上轨，**且**该 1H 最高价是过去 24 根 1H 的最高价，**且** 1H 收盘价位于 6H 布林中轨上方
   - 做空：1H 收盘价跌破 1H 布林下轨，**且**该 1H 最低价是过去 24 根 1H 的最低价，**且** 1H 收盘价位于 6H 布林中轨下方
   - 6H 中轨过滤由 `H6_MIDDLE_FILTER_ENABLED` 开关控制（默认开启）
   - 信号在 1H 收盘确认，下一根 1M 开盘价（市价单）入场

4. **日线阶段闸门 + 同阶段只做第一笔**（`PHASE_FILTER_ENABLED`，默认开启）：在上述信号之上再叠加一层日线布林（20,2）阶段过滤，**不改原买点，只决定信号是否允许做**——
   - 上涨阶段（日线收盘突破布林上轨开始，跌破中轨结束）内**只允许做多**；下跌阶段（跌破下轨开始，涨破中轨结束）内**只允许做空**；不符方向的信号过滤掉
   - 同一币种、同一阶段**只做第一笔**有效信号，之后同向信号全部跳过，直到进入新阶段才重新计算
   - 阶段只用已收盘日 K 判定（无未来函数）；"本阶段已交易"标记持久化到 `state.json`，重启沿用

5. **交易量优先**：多信号并发时，按 24h 成交量从大到小排序开仓，优先流动性好的币种。

### 出场逻辑

出场机制由 `EXIT_MODE` 控制，默认 `phase_bb`。

#### `phase_bb`（默认）— 1H 收盘出场

每根 1H 收盘后检查一次，满足任一条件即市价平仓：

| 出场条件 | 说明 |
|------|------|
| 1H 布林中轨穿越 | 多单：1H 收盘价跌破 1H 布林中轨（20,2）；空单：涨破中轨 |
| 3.5% 确认回撤 | 多单：从入场以来**已确认**的最高点回撤 ≥ 3.5%；空单：从最低点反弹 ≥ 3.5%（`PHASE_EXIT_TRAILING_PCT`） |

- "已确认极值"只取**当前 1H 之前**已形成的高/低点（排除当前 K 与入场 K），避免同一根 K 内先创新高又立刻按该高低点触发回撤的次序失真。
- 仓位规模沿用等风险名义（与 `atr_dual` 同源：名义 = `RISK_PER_TRADE_USD` / 软止损%，封顶 `MAX_NOTIONAL_USD`）。
- **灾难止损**：另在交易所挂一道很宽的 `STOP_MARKET`（`CATASTROPHE_STOP_PCT`，默认 8%），**仅**用于 bot 掉线/跳空兜底——正常情况下中轨/3.5% 出场会先触发。可用 `CATASTROPHE_STOP_ENABLED=False` 关闭（关闭后该仓只靠相位出场，不挂任何止损）。

> ⚠️ `phase_bb` 是**回测为负**（2025-06→2026-06 约 −12.4%、毛 edge≈0）的前向观察配置，**不是盈利预期**；实现与回测结论见 `docs/superpowers/plans/2026-06-27-phase-filter-live.md`。

#### `atr_dual` / `fixed`（传统模式，`EXIT_MODE="atr_dual"` 切回）

止损细分由 `STOP_MODE` 控制：

- **软止损**（`atr_dual`）：ATR 自适应距离 `max(2%, 1.5×ATR14(1H)/价)`，仅在 1H 收盘确认（本地，每小时一次）。
- **硬止损**：`min(2×软, 6%)`，开仓即在交易所挂 `STOP_MARKET`（防掉线/跳空）。
- **激活式移动止盈**：浮盈达 `TRAILING_ACTIVATION_PCT`（3.5%）后挂 `TRAILING_STOP_MARKET`，回撤 `TRAILING_DRAWDOWN_PCT`（1.5%）出场；`watcher.py` 的 WebSocket 实时激活 + 每 60 秒轮询兜底。两个交易所订单构成手动 OCO，任一触发自动撤销另一个。
- `STOP_MODE="fixed"`：回退到旧的固定 2% 止损（`FIXED_STOP_LOSS_PCT`）+ 移动止盈。

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

#### BNB 抵扣（9 折）

- 开关 `BNB_FEE_BURN_ENABLED`（默认关闭）。开启后启动时调 `POST /fapi/v1/feeBurn` 同步 Binance 侧开关为 ON，所有 USDⓈ-M 永续合约的手续费打 9 折
- 启动 + 每次心跳检查合约钱包的 BNB 余额，低于 `BNB_BALANCE_MIN_ALERT`（默认 0.05 BNB）时发警告通知
- **Bot 不会自动买/划转 BNB**——你需要手动把 BNB 划到 USDⓈ-M Futures 钱包；BNB 不足时 Binance 自动回退到 USDT 扣费（当笔不享受折扣）
- 关闭开关时 bot 不会主动改 Binance 侧设置（保留你手动设的状态）

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
- 启动时**不立即开仓**，等待到下一个整点 `:00:05` 才执行首次扫描，启动日志和通知中会显示首次扫描时间及倒计时
- 启动时立即检查已有持仓的止损/止盈状态
- 每小时 `:00:05` 扫描市场前先做**日内跌幅检查**和 **1H bar 闸门校验**（探测 BTCUSDT 确认最新已收盘 bar 已发布），然后并发预取 50 个 symbol 的 K 线数据（典型耗时 2–3s），跌幅 ≥ 20% 自动触发冷静期
- 每 60 秒检查持仓止损/止盈订单状态
- WebSocket 后台线程实时监控标记价格，触发移动止盈激活
- 每 4 小时推送策略执行汇报（含当前单仓金额、今日快照、日内盈亏变动）
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

### 出场 / 止损止盈

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `EXIT_MODE` | "phase_bb" | 出场模式：`phase_bb`（1H 中轨穿越 + 3.5% 确认回撤）/ `atr_dual`（双层 ATR 止损 + 移动止盈） |
| `PHASE_EXIT_TRAILING_PCT` | 0.035 | phase_bb：从确认极值回撤/反弹触发出场（3.5%） |
| `CATASTROPHE_STOP_ENABLED` | True | phase_bb：是否保留宽幅灾难止损（掉线/跳空兜底） |
| `CATASTROPHE_STOP_PCT` | 0.08 | 灾难止损距离（8%） |
| `STOP_MODE` | "atr_dual" | atr_dual 模式止损：`atr_dual`（软/硬双层）/ `fixed`（固定 2%） |
| `SOFT_STOP_ATR_MULT` | 1.5 | 软止损 = 1.5 × ATR14(1H) / 价（下限 `SOFT_STOP_FLOOR_PCT` 2%，上限 6%） |
| `HARD_STOP_CAP_PCT` | 0.06 | 硬止损上限（= 2×软，封顶 6%；软止损也以此封顶） |
| `RISK_PER_TRADE_USD` | 40 | 单笔风险额；等风险名义 = 40 / 软止损%（phase_bb 与 atr_dual 同用） |
| `MAX_NOTIONAL_USD` | 2000 | 等风险名义上限 |
| `TRAILING_ACTIVATION_PCT` | 0.035 | 移动止盈激活阈值（浮盈≥3.5%，atr_dual 模式） |
| `TRAILING_DRAWDOWN_PCT` | 0.015 | 移动止盈激活后回撤触发线（1.5%） |
| `FIXED_STOP_LOSS_PCT` | 0.02 | 固定止损（仅 `STOP_MODE="fixed"`） |

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
| `PHASE_FILTER_ENABLED` | True | 日线 BB 阶段闸门 + 同阶段只做第一笔（叠加在入场信号之上，详见入场逻辑） |
| `PHASE_BB_PERIOD` / `PHASE_BB_STD` | 20 / 2.0 | 日线阶段布林参数 |
| `PHASE_DAILY_LOOKBACK` | 250 | 重放阶段时间线所需的日线根数 |
| `BNB_FEE_BURN_ENABLED` | False | 开启 BNB 抵扣手续费（9 折）；启动时同步 Binance 侧 `feeBurn` 开关为 ON |
| `BNB_BALANCE_MIN_ALERT` | 0.05 | 合约钱包 BNB 余额低于此值时通知告警（仅在 `BNB_FEE_BURN_ENABLED=True` 时生效） |

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
