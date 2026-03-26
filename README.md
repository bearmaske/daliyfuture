# Trend Sniper — Binance Testnet 模拟盘交易 Bot

基于布林带的加密货币 USDT 永续合约量化交易策略，运行在 Binance Testnet（测试网）上。

## 策略简介

- **日线布林带**判断趋势方向（价格 > 中轨做多，< 中轨做空）
- **小时线布林带**捕捉突破入场（突破上/下轨 + 成交量放大）
- **双重平仓机制**：
  - 移动止损（多单回撤 3%，空单反弹 5%）
  - 价格回归 1H 布林中轨自动平仓
- 每小时自动扫描成交量前 30 的 USDT 永续合约
- 启动时自动从 Testnet 同步账户数据（余额 + 持仓）

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
# 必填 — Binance Testnet API 密钥
# 申请地址：https://testnet.binancefuture.com
BINANCE_TESTNET_API_KEY=你的key
BINANCE_TESTNET_API_SECRET=你的secret

# 可选 — Telegram 通知
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=你的bot_token
TELEGRAM_CHAT_ID=你的chat_id

# 可选 — Bark 通知（iOS 推送，支持多设备，逗号分隔）
BARK_ENABLED=false
BARK_URLS=https://api.day.app/key1,https://api.day.app/key2
```

> **Telegram 和 Bark 默认关闭。** 需要哪个就把对应的 `_ENABLED` 改为 `true` 并填入密钥。

### 3. 启动 Bot

```bash
source .venv/bin/activate
python main.py
```

Bot 启动后会持续运行：
- 启动时立即执行一次策略扫描和止损检查
- 每小时 :01 扫描市场并检查入场信号
- 每 2 分钟检查持仓止损 + 布林中轨平仓
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
├── exchange.py      # Binance API 封装（主网行情 + 测试网下单 + 账户同步）
├── strategy.py      # 布林带策略（趋势判断 + 入场信号）
├── risk.py          # 移动止损 + 布林中轨平仓
├── notifier.py      # 日志 + Telegram + Bark 通知
├── state.py         # JSON 状态持久化 + Testnet 同步
├── tests/           # 单元测试
├── .env.example     # 环境变量模板
├── requirements.txt # Python 依赖
└── state.json       # 运行时状态（自动生成）
```

## 策略参数

所有参数在 `config.py` 中定义，可直接修改：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `INITIAL_CAPITAL` | 10000 | 初始资金（USDT） |
| `POSITION_SIZE` | 500 | 单仓保证金（USDT） |
| `MAX_POSITIONS` | 10 | 最大同时持仓数 |
| `LEVERAGE` | 5 | 杠杆倍数 |
| `LONG_TRAILING_STOP` | 0.03 | 多单移动止损（从最高价回撤 3%） |
| `SHORT_TRAILING_STOP` | 0.05 | 空单移动止损（从最低价反弹 5%） |
| `BB_PERIOD` | 20 | 布林带周期 |
| `BB_STD` | 2.0 | 布林带标准差倍数 |
| `TOP_SYMBOLS_COUNT` | 30 | 扫描成交量前 N 币种 |
| `RISK_CHECK_INTERVAL_MINUTES` | 2 | 止损检查间隔（分钟） |

## 平仓逻辑

Bot 有两种平仓触发方式，每 2 分钟检查一次：

1. **移动止损**：多单从最高价回撤 >= 3%，或空单从最低价反弹 >= 5%
2. **回归中轨**：价格回到 1H 布林中轨（多单跌破中轨 / 空单涨破中轨），即使未触发止损也平仓

平仓通知会标注触发原因（"移动止损" 或 "回归中轨"）。

## 数据同步

- 启动时和每次策略扫描前，自动从 Testnet 同步真实账户数据
- **余额**以 Testnet 实际可用余额为准
- **持仓**以 Testnet 实际持仓为准（自动新增/移除本地不一致的仓位）
- **行情数据**从 Binance 主网获取（数据质量更好）
- **下单执行**通过 Testnet 进行（不涉及真实资金）

## 策略执行汇报

每 6 小时自动推送一次汇报，内容包含：

```
余额: $9538.26 | 持仓: 2/10
已平仓: 3 笔 | 胜率: 67% (2胜/1负)
累计已实现PnL: $38.26
--- 当前持仓 ---
DOTUSDT SHORT | 入场: 1.3890 | 现价: 1.3740 | +$54.00
BTCUSDT LONG | 入场: 70000.0000 | 现价: 70500.0000 | +$17.86
未实现PnL合计: +$71.86
--- 策略状态 ---
策略运行正常
```

## 日志

- 终端实时输出 + 写入 `binance_paper_trading.log` 文件
- 启动时打印完整配置摘要、通知渠道状态、调度信息
- 策略扫描详细记录每个币种的趋势、布林带数值、量比、信号
- 止损检查记录每个持仓的回撤/反弹百分比、中轨位置

## 注意事项

- 这是**模拟盘**，不涉及真实资金
- Binance Testnet API Key 需要在 [testnet.binancefuture.com](https://testnet.binancefuture.com) 单独申请
- Testnet 的撮合引擎和主网独立，订单簿可能较薄
- 建议用 `screen` 或 `tmux` 在后台运行 Bot
