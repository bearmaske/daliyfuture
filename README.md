# Trend Sniper — Binance 加密货币合约交易 Bot

基于双时间框架布林带的加密货币 USDT 永续合约动量策略。支持模拟盘（Testnet）和实盘（Mainnet）两种模式，通过环境变量一键切换。

## 策略简介

### 入场逻辑

三层过滤 + 优先排序：

1. **选币**：取成交量前 50 的 USDT 永续合约，排除稳定币对
2. **日线趋势判断**（可配置模式，`TREND_FILTER_MODE`）：
   - `sma`（默认）：SMA 斜率方向 + 价格位置 → 判断做多/做空/跳过
   - `bb_middle`：价格 > 日线布林中轨 → 只做多，< 中轨 → 只做空
   - `disabled`：不做日线过滤，仅靠小时线突破方向决定多空
3. **波动率过滤**（`VOL_FILTER_ENABLED`）：比较短期 ATR(7) 与长期 ATR(28) 的比值，低于阈值说明波动率收缩（震荡市），跳过开仓
4. **小时线布林带突破确认**：
   - 做多方向：1H 收盘价突破上轨 → 开多
   - 做空方向：1H 收盘价跌破下轨 → 开空
5. **交易量优先开仓**：当同时出现多个信号时，先收集所有信号，按 24h 交易量从大到小排序，优先开仓交易量大的币种

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

- 开仓和平仓时从 Binance 订单成交记录中提取**平台实际收取的手续费**
- 自动处理 USDT 和 BNB 抵扣两种手续费类型
- PnL 计算已扣除开仓+平仓双边手续费，显示的是净盈亏
- 策略汇报中统计累计手续费支出

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

# 可选 — Telegram 通知
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=你的bot_token
TELEGRAM_CHAT_ID=你的chat_id

# 可选 — Bark 通知（iOS 推送，支持多设备，逗号分隔）
BARK_ENABLED=false
BARK_URLS=https://api.day.app/key1,https://api.day.app/key2
```

> **默认模拟盘模式。** 切换实盘：将 `TRADING_MODE` 改为 `live` 并填入主网 API 密钥。
> **Telegram 和 Bark 默认关闭。** 需要哪个就把对应的 `_ENABLED` 改为 `true` 并填入密钥。

### 3. 启动 Bot

```bash
source .venv/bin/activate
python main.py
```

Bot 启动后会持续运行：
- 启动时立即执行一次策略扫描和止损检查
- 每小时 :01 扫描市场并检查入场信号
- 每 1 分钟检查持仓 ATR 移动止损
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
├── notifier.py      # 日志 + Telegram + Bark 通知
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
| `TREND_FILTER_MODE` | "sma" | 日线趋势过滤模式：`sma` / `bb_middle` / `disabled` |
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
| `RISK_CHECK_INTERVAL_MINUTES` | 1 | 止损检查间隔（分钟） |

## 策略执行汇报

每 6 小时自动推送一次汇报，内容包含：

```
--- 资产概览 ---
总资产: $10538.26 | 盈利: +$538.26 (+5.38%)
钱包余额: $10400.00 | 可用余额: $9400.00
持仓未实现PnL: +$138.26
初始资金: $10000.00
--- 交易统计 (实盘) ---
持仓: 2/10
已平仓: 3 笔 | 胜率: 67% (2胜/1负)
累计已实现PnL: +$400.00 | 累计手续费: $2.4000
--- 当前持仓 ---
DOTUSDT SHORT | 入场: 1.3890 | 现价: 1.3740 | +$54.00 (+10.8%)
BTCUSDT LONG | 入场: 70000.0000 | 现价: 70500.0000 | +$17.86 (+3.6%)
--- 策略状态 ---
策略运行正常
```

## 日志

- 终端实时输出 + 写入 `binance_paper_trading.log` 文件
- 启动时打印完整配置摘要、通知渠道状态、调度信息
- 策略扫描详细记录每个币种的趋势、布林带数值、信号
- 止损检查记录每个持仓的 ATR 值、止损线、回撤百分比

## 注意事项

- **默认模拟盘**，不涉及真实资金。切换实盘请确认已充分了解策略风险
- 实盘 API Key 建议：**只开合约交易权限，不开提币权限，绑定 IP 白名单**
- Binance Testnet API Key 需要在 [testnet.binancefuture.com](https://testnet.binancefuture.com) 单独申请
- Testnet 的撮合引擎和主网独立，订单簿可能较薄
- 建议用 `tmux` 在后台运行 Bot
